"""Properties of the usefulness and decay engine (ADR 0006 §4-§5).

Property-based in the repo's own style: seeded `random`, many generated cases
per property, no new dependency (CONTRIBUTING: a dependency has to earn its
place). A failure prints the seed, and the same seed replays the same case.

Every trace here is SYNTHETIC. No real run data is used or needed.
"""

from __future__ import annotations

import random

import pytest

from agentco import memory_engine as engine
from agentco.memory_engine import Params, State, Trace

P = Params()
SEEDS = range(40)


def random_world(rng: random.Random, n: int = 8) -> dict[str, State]:
    states = {}
    for i in range(n):
        state = engine.initial(f"m{i}", P, pinned=rng.random() < 0.25)
        if not state.pinned and rng.random() < 0.2:
            state = State.from_dict({**state.to_dict(), "cold": True, "strength": 0.06})
        states[state.item_id] = state
    return states


def random_trace(rng: random.Random, ids: list[str], n: int) -> Trace:
    retrieved = frozenset(i for i in ids if rng.random() < 0.6)
    invoked = frozenset(i for i in retrieved if rng.random() < 0.4)
    return Trace(f"t{n}", rng.choice(engine.OUTCOMES), invoked, retrieved)


def run(states, traces):
    all_credits = []
    for trace in traces:
        states, credits = engine.apply_trace(states, trace, P)
        all_credits.extend(credits)
    return states, all_credits


def traces_for(seed: int, ids: list[str], count: int = 60) -> list[Trace]:
    rng = random.Random(seed)
    return [random_trace(rng, ids, n) for n in range(count)]


# --------------------------------------------------------------------------- #
# properties
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("seed", SEEDS)
def test_never_deletes(seed):
    rng = random.Random(seed)
    states = random_world(rng)
    after, _ = run(states, traces_for(seed, list(states)))
    assert set(after) == set(states), f"seed {seed}"
    for item_id in states:
        assert engine.activation(after[item_id], P) >= P.floor, f"seed {seed}: below the floor"


@pytest.mark.parametrize("seed", SEEDS)
def test_pinned_never_decays_or_reinforces(seed):
    rng = random.Random(seed)
    states = random_world(rng)
    after, credits = run(states, traces_for(seed, list(states)))
    for item_id, before in states.items():
        if before.pinned:
            assert after[item_id] == before, f"seed {seed}: a pinned item changed"
            assert engine.activation(after[item_id], P) == 1.0
            assert all(c.effect == "pinned" for c in credits if c.item_id == item_id)
            for up in (True, False):
                assert engine.rate(before, up, P) == before


@pytest.mark.parametrize("seed", SEEDS)
def test_only_invoked_items_with_a_verified_outcome_get_credit(seed):
    rng = random.Random(seed)
    states = random_world(rng)
    ids = list(states)
    for trace in traces_for(seed, ids, 30):
        after, credits = engine.apply_trace(states, trace, P)
        for item_id in ids:
            b, a = states[item_id], after[item_id]
            if item_id not in trace.invoked:
                assert a.strength == b.strength and a.successes == b.successes and a.failures == b.failures, \
                    f"seed {seed}: credit to an item that was not invoked"
            if item_id in trace.invoked and trace.outcome == "unverified":
                assert a == b, f"seed {seed}: an unverified outcome changed something"
            if item_id in trace.invoked and trace.outcome == "failure" and not (b.pinned or b.cold):
                assert a.failures == b.failures + 1 and a.strength == b.strength, \
                    f"seed {seed}: a failure is recorded and does not reinforce"
        for c in credits:
            if c.effect in ("success", "failure"):
                assert c.invoked and c.outcome in ("success", "failure")
        states = after


@pytest.mark.parametrize("seed", SEEDS)
def test_decay_is_monotonic_without_credit(seed):
    """Exposure alone — retrieved, never invoked — never raises activation,
    never takes it below the floor, and eventually reaches cold."""
    import math

    rng = random.Random(seed)
    strength = rng.uniform(P.cold_threshold + 0.01, 2.0)
    state = State("m", strength=strength)
    previous = engine.activation(state, P)
    # The power law has a heavy tail: the exposures to cold are derived from
    # the parameters, not guessed — strength·(1+a/scale)^-d = threshold.
    bound = math.ceil(P.age_scale * ((strength / P.cold_threshold) ** (1 / P.decay_exponent) - 1)) + 1
    for n in range(bound):
        state, _ = engine.apply_trace({"m": state}, Trace(f"t{n}", rng.choice(engine.OUTCOMES),
                                                           frozenset(), frozenset({"m"})), P)
        state = state["m"]
        current = engine.activation(state, P)
        assert current <= previous + 1e-12, f"seed {seed}: activation rose without credit"
        assert current >= P.floor
        previous = current
        if state.cold:
            break
    assert state.cold, f"seed {seed}: not cold after the {bound} exposures the parameters imply"


@pytest.mark.parametrize("seed", SEEDS)
def test_revival_returns_on_probation(seed):
    rng = random.Random(seed)
    cold = State("m", strength=rng.uniform(0.01, 0.1), age=rng.randint(0, 500), cold=True)
    revived = engine.revive(cold, P)
    assert not revived.cold and revived.probation_left == P.probation_successes
    assert revived.reconsolidate and revived.age == 0
    assert engine.activation(revived, P) > P.cold_threshold
    warm = engine.initial("w", P)
    assert engine.revive(warm, P) == warm, "only a cold item revives"

    # A verified failure while on probation sends it straight back to cold.
    back, _ = engine.apply_trace({"m": revived}, Trace("f", "failure", frozenset({"m"}), frozenset({"m"})), P)
    assert back["m"].cold and back["m"].probation_left == 0
    # Enough verified successes end probation.
    state = revived
    for n in range(P.probation_successes):
        state = engine.apply_trace({"m": state}, Trace(f"s{n}", "success", frozenset({"m"}),
                                                       frozenset({"m"})), P)[0]["m"]
    assert state.probation_left == 0 and not state.cold


@pytest.mark.parametrize("seed", SEEDS)
def test_determinism_for_a_fixed_trace(seed):
    rng = random.Random(seed)
    states = random_world(rng)
    traces = traces_for(seed, list(states))
    first = run(states, traces)
    second = run(dict(reversed(list(states.items()))), [
        Trace(t.trace_id, t.outcome, frozenset(sorted(t.invoked, reverse=True)),
              frozenset(sorted(t.retrieved, reverse=True))) for t in traces])
    assert first[0] == second[0], f"seed {seed}"
    assert first[1] == second[1], f"seed {seed}"


def test_a_cold_item_named_in_a_trace_is_neither_used_nor_exposed():
    cold = State("m", strength=0.06, cold=True)
    after, credits = engine.apply_trace({"m": cold}, Trace("t", "success", frozenset({"m"}), frozenset({"m"})), P)
    assert after["m"] == cold and credits[0].effect == "cold-skip"


def test_the_rating_outweighs_an_automatic_credit():
    base = engine.initial("m", P)
    rated = engine.rate(base, True, P)
    credited = engine.apply_trace({"m": base}, Trace("t", "success", frozenset({"m"}), frozenset({"m"})), P)[0]["m"]
    assert rated.strength - base.strength > credited.strength - base.strength
    down = engine.rate(base, False, P)
    assert down.strength < base.strength and down.reconsolidate


def test_replay_picks_the_weakest_cold_items_deterministically():
    states = [State(f"m{i}", strength=0.05 + i * 0.001, cold=True) for i in range(8)]
    states.append(State("warm", strength=1.0))
    states.append(State("pinned", strength=0.01, cold=True, pinned=True))
    chosen = engine.replay_candidates(list(reversed(states)), P)
    assert chosen == [f"m{i}" for i in range(P.replay_batch)]


def test_every_parameter_is_configuration_and_labelled_not_validated():
    described = Params().describe()
    assert "NOT VALIDATED" in described["origin"]
    custom = Params.from_env('{"decay_exponent": 0.8, "floor": 0.01}')
    assert custom.decay_exponent == 0.8 and custom.floor == 0.01
    with pytest.raises(ValueError):
        Params.from_env('{"half_life_days": 29}')
    with pytest.raises(ValueError):
        Params(floor=0.5, cold_threshold=0.1)


# --------------------------------------------------------------------------- #
# simulation — synthetic traces only
# --------------------------------------------------------------------------- #


def test_simulation_use_keeps_items_warm_and_disuse_cools_them_without_loss():
    """Four synthetic items over 1,200 synthetic runs (with the default
    parameters an item at strength 1.0 goes cold after 990 exposures — the
    power law's tail is long, which is itself something to measure):

      * `useful`   — retrieved every run, invoked in a verified success every 5th;
      * `ignored`  — retrieved every run, never invoked;
      * `rare`     — a pinned constraint, retrieved every run, invoked twice;
      * `dormant`  — never retrieved: no exposure, so no ageing at all.

    Then a replay pass revives the cold ones on probation, and one verified
    failure on probation returns the revived item to cold.
    """
    params = Params()
    states = {
        "useful": engine.initial("useful", params),
        "ignored": engine.initial("ignored", params),
        "rare": engine.initial("rare", params, pinned=True),
        "dormant": engine.initial("dormant", params),
    }
    for n in range(1200):
        invoked = set()
        if n % 5 == 0:
            invoked.add("useful")
        if n in (100, 300):
            invoked.add("rare")
        trace = Trace(f"sim-{n}", "success", frozenset(invoked), frozenset({"useful", "ignored", "rare"}))
        states, _ = engine.apply_trace(states, trace, params)

    assert not states["useful"].cold and states["useful"].successes == 240
    assert states["ignored"].cold and states["ignored"].exposures >= 1
    assert states["rare"] == engine.initial("rare", params, pinned=True)
    assert states["dormant"] == engine.initial("dormant", params), "a quiet stretch erodes nothing"
    assert set(states) == {"useful", "ignored", "rare", "dormant"}

    chosen = engine.replay_candidates(states.values(), params)
    assert chosen == ["ignored"]
    revived = engine.revive(states["ignored"], params)
    assert revived.probation_left == params.probation_successes and revived.reconsolidate
    failed, _ = engine.apply_trace({"ignored": revived},
                                   Trace("sim-fail", "failure", frozenset({"ignored"}), frozenset({"ignored"})),
                                   params)
    assert failed["ignored"].cold and failed["ignored"].failures == 1
