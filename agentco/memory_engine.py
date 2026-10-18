"""Usefulness and decay: a pure, deterministic engine (ADR 0006 §4, §5).

No I/O, no wall clock, no randomness. Every function takes a state and an
event and returns a new state, so a fixed sequence of traces gives the same
result on every machine, every time — which is what lets the property tests
and the simulation in `tests/` say anything at all.

**The clock is exposure, not the calendar.** An item ages only when it was
available to a run and not used: it was in the run's `retrieved` set and not in
its `invoked` set. A quiet month moves nothing.

**Credit only what was used, against an externally verified outcome.** A
trace names the items the run *invoked* — that demonstrably shaped an action —
and carries an outcome the plane verified, never one the reporter asserted:

  * `success`  — strength rises, age resets;
  * `failure`  — age resets (a use is a use, and failures consolidate too), the
                 failure is counted, strength does NOT rise: a lesson invoked in
                 a failure is evidence, not endorsement;
  * `unverified` — nothing changes at all. Not even an exposure.

**The owner's rating is the strong event**: one rating moves strength more than
any automatic credit (`rating_gain` ≫ `success_gain`).

**Power-law decay with an activation floor** (§5):

    activation = max(floor, strength · (1 + age / age_scale) ^ (−decay_exponent))

**Decay never deletes.** An item whose activation falls to `cold_threshold`
goes COLD: kept, auditable, not offered to runs. It comes back by **demand
pull** (someone asks for it) or **periodic replay** (a scheduled pass picks
the coldest), and returns **on probation**, flagged for reconsolidation (§4): a
verified failure while on probation sends it straight back to cold;
`probation_successes` verified successes end probation.

**Pinned items** are exempt from decay AND reinforcement: no event changes
their strength, age or activation. Use is the wrong signal for a constraint;
they are reviewed on the calendar, by a person (`memory.py`, `review_by`).

**Every number here is a configuration value, and none is validated.** ADR
0006: "No parameter is validated." The defaults are engineering choices made
for this implementation, labelled as such in `Params.ORIGIN`, and a deployment
overrides them with `AGENTCO_MEMORY_PARAMS` (JSON).
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, fields, replace
from typing import Iterable, Mapping, Optional

OUTCOMES = ("success", "failure", "unverified")
PARAMS_ENV_VAR = "AGENTCO_MEMORY_PARAMS"


@dataclass(frozen=True)
class Params:
    """Engine parameters. NOT VALIDATED — see ORIGIN."""

    decay_exponent: float = 0.5
    age_scale: float = 10.0
    floor: float = 0.05
    cold_threshold: float = 0.1
    initial_strength: float = 1.0
    success_gain: float = 0.5
    rating_gain: float = 2.0
    rating_down_factor: float = 0.5
    revive_strength: float = 0.5
    probation_successes: int = 3
    replay_batch: int = 5
    pinned_review_days: int = 90

    ORIGIN = (
        "NOT VALIDATED. Engineering defaults chosen for this implementation on "
        "2026-10-17, from no measurement; ADR 0006 states that no parameter is "
        "validated. Override with AGENTCO_MEMORY_PARAMS and record the origin of "
        "any value you set."
    )

    def __post_init__(self) -> None:
        if not 0 < self.floor <= self.cold_threshold < self.initial_strength:
            raise ValueError("need 0 < floor <= cold_threshold < initial_strength")
        if not self.revive_strength > self.cold_threshold:
            # Otherwise a revived item would be cold again on arrival.
            raise ValueError("revive_strength must exceed cold_threshold")
        if self.decay_exponent <= 0 or self.age_scale <= 0:
            raise ValueError("decay_exponent and age_scale must be positive")
        if self.success_gain < 0 or self.rating_gain < 0 or not 0 < self.rating_down_factor <= 1:
            raise ValueError("gains are non-negative; rating_down_factor is in (0, 1]")
        if self.probation_successes < 1 or self.replay_batch < 1 or self.pinned_review_days < 1:
            raise ValueError("probation_successes, replay_batch and pinned_review_days are at least 1")

    def describe(self) -> dict:
        return {"values": asdict(self), "origin": self.ORIGIN}

    @classmethod
    def from_env(cls, raw: Optional[str] = None) -> "Params":
        raw = raw if raw is not None else os.environ.get(PARAMS_ENV_VAR, "")
        if not raw.strip():
            return cls()
        data = json.loads(raw)
        known = {f.name for f in fields(cls)}
        unknown = set(data) - known
        if unknown:
            raise ValueError(f"{PARAMS_ENV_VAR}: unknown parameter(s) {sorted(unknown)}")
        return cls(**data)


@dataclass(frozen=True)
class State:
    item_id: str
    strength: float
    age: int = 0                 # exposures since the last use
    exposures: int = 0           # every exposure, ever
    successes: int = 0
    failures: int = 0
    ratings_up: int = 0
    ratings_down: int = 0
    cold: bool = False
    probation_left: int = 0
    reconsolidate: bool = False
    pinned: bool = False

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: Mapping) -> "State":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in raw.items() if k in known})


@dataclass(frozen=True)
class Trace:
    trace_id: str
    outcome: str
    invoked: frozenset
    retrieved: frozenset = frozenset()

    def __post_init__(self) -> None:
        if self.outcome not in OUTCOMES:
            raise ValueError(f"outcome must be one of {OUTCOMES}")


@dataclass(frozen=True)
class Credit:
    """What one trace did to one item — the credit history row."""

    trace_id: str
    item_id: str
    invoked: bool
    outcome: str
    effect: str   # success | failure | none | exposure | pinned | cold-skip


def initial(item_id: str, params: Params, *, pinned: bool = False) -> State:
    return State(item_id=item_id, strength=params.initial_strength, pinned=pinned)


def activation(state: State, params: Params) -> float:
    if state.pinned:
        return 1.0
    raw = state.strength * (1.0 + state.age / params.age_scale) ** (-params.decay_exponent)
    return max(params.floor, raw)


def _settle(state: State, params: Params) -> State:
    """Cold when activation reaches the threshold. Never the reverse: only a
    revival warms an item, so going cold is not undone by arithmetic."""
    if not state.pinned and not state.cold and activation(state, params) <= params.cold_threshold:
        return replace(state, cold=True, probation_left=0)
    return state


def apply_trace(states: Mapping[str, State], trace: Trace, params: Params) -> tuple[dict, list[Credit]]:
    """One trace over the states it names. Returns the new states (every input
    state, changed or not — nothing is ever dropped) and the credit rows.

    Items in neither set are untouched. An id the states do not hold is
    ignored: the caller decides what an unknown id means.
    """
    out = dict(states)
    credits: list[Credit] = []
    for item_id in sorted(set(trace.invoked) | set(trace.retrieved)):
        state = out.get(item_id)
        if state is None:
            continue
        invoked = item_id in trace.invoked
        if state.pinned:
            effect = "pinned"
        elif state.cold:
            # A cold item is not offered to runs. If a trace names it anyway,
            # that is not use and not exposure; revival is an explicit act.
            effect = "cold-skip"
        elif invoked and trace.outcome == "success":
            on_probation = state.probation_left > 0
            state = replace(
                state, strength=state.strength + params.success_gain, age=0,
                successes=state.successes + 1,
                probation_left=state.probation_left - 1 if on_probation else 0,
            )
            effect = "success"
        elif invoked and trace.outcome == "failure":
            if state.probation_left > 0:
                state = replace(state, failures=state.failures + 1, cold=True, probation_left=0)
            else:
                state = replace(state, age=0, failures=state.failures + 1)
            effect = "failure"
        elif invoked:
            effect = "none"
        else:
            state = replace(state, age=state.age + 1, exposures=state.exposures + 1)
            effect = "exposure"
        out[item_id] = _settle(state, params)
        credits.append(Credit(trace.trace_id, item_id, invoked, trace.outcome, effect))
    return out, credits


def rate(state: State, up: bool, params: Params) -> State:
    """The owner's rating. Pinned items take none; a down-rating weakens and
    flags for reconsolidation, and never deletes."""
    if state.pinned:
        return state
    if up:
        return _settle(replace(state, strength=state.strength + params.rating_gain, age=0,
                               ratings_up=state.ratings_up + 1), params)
    return _settle(replace(state, strength=state.strength * params.rating_down_factor,
                           ratings_down=state.ratings_down + 1, reconsolidate=True), params)


def revive(state: State, params: Params) -> State:
    """Bring a cold item back, on probation and flagged for reconsolidation.
    A warm item is returned unchanged: there is nothing to revive."""
    if not state.cold:
        return state
    return replace(state, cold=False, strength=params.revive_strength, age=0,
                   probation_left=params.probation_successes, reconsolidate=True)


def reconsolidated(state: State) -> State:
    return replace(state, reconsolidate=False)


def replay_candidates(states: Iterable[State], params: Params) -> list[str]:
    """The cold items a periodic replay should re-test: the weakest first,
    then by id, `replay_batch` of them. Deterministic."""
    cold = [s for s in states if s.cold and not s.pinned]
    cold.sort(key=lambda s: (activation(s, params), s.item_id))
    return [s.item_id for s in cold[: params.replay_batch]]
