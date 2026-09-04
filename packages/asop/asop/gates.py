"""The unified gate schema — the ASOP contract's `verified` property, in one shape.

A procedure that says what done means, and cannot tell whether done happened,
is a document. The gate is the difference: a declared check, attached to a
unit of work, that must produce evidence before the unit counts as complete.

This module is a merge of two gate schemas that grew up independently and
converge on the same three-way split — `deterministic`, `judged`, `human` —
under two different names for the same axis (`agentco/gates.py` called it
`kind`; the AgentCo Harness called it `class`) and with disjoint optional
fields either side never needed: the plane's park clock
(`max_park_seconds`/`on_timeout`/`escalate_to`/`verifier`) and the Harness's
runtime hints (`cwd`/`timeout_s`/`rubric`/`judge_route`). Neither side is
wrong; a gate authored once and read by both needs one normalised shape, not
two compatible-by-convention ones.

**`kind` is canonical; `class` is a read alias.** A caller may send either.
Sending both is refused if they disagree — silently preferring one would mean
the payload the author wrote and the gate that got stored say different
things, and nobody would know which. Normalised output always carries `kind`.

**Three field groups**, because they answer three different questions:

  `core` — always required. `kind`, and exactly one of `check` (one command)
      or `checks` (an ordered, staged list — the Harness's contribution: a
      lint→types→unit→integration ladder that stops at the first failure and
      names which stage broke, rather than one opaque `&&`-chained command
      that only says pass or fail).

  `clock` — the plane's park clock: how long a gate may sit unanswered
      before `on_timeout` decides its fate, and who a `human` gate is
      addressed to. **Declare all of it or none of it** — a partially
      declared clock is refused regardless of whether the caller is required
      to have one at all, because a clock that is half-configured behaves
      like a bug that will not reproduce: `max_park_seconds` with no
      `on_timeout` parks forever by omission rather than by anyone's decision.

  `execution` — the Harness's runtime hints for actually running the check:
      `cwd`, `timeout_s`, and, for a `judged` gate, `rubric` (what to weigh)
      and `judge_route` (where the judgment happens). `rubric` on a
      `deterministic` gate and `judge_route` on anything but `judged` are
      refused for the same reason a `verifier` on a `deterministic` gate is:
      a field nobody reads is not documentation, it is a promise the gate
      does not keep.

**Whether the clock group is mandatory is the one thing that differs between
callers**, and it is the one knob this module exposes: `require=("clock",)`
is the plane's calling convention today (every gate declares its park
clock), `require=()` is a caller — the Harness, from P2 — whose gates have no
concept of parking yet. Decomposition invariants in, numbers out: this
function has no opinion on how long a park may run. `max_park_seconds_ceiling`
is an argument, not a constant, because "thirty days" is the plane's
decision about its own registry, not a property of what a gate *is*. A
caller with no ceiling in mind passes `None` and none is enforced.

**A malformed gate is refused at the write boundary, never stored.** This is
the whole reason validation lives here rather than at the point of use: a
gate that quietly does nothing is worse than no gate at all, because it
reports green. So an unknown key is a refusal rather than a field nobody
reads — the usual forward-compatibility argument (a store tolerating an
`unknown` bucket) is about a field a NEWER writer added, while an unknown key
in a gate is overwhelmingly a typo, and the cost of guessing wrong is a check
that never runs and nobody misses.
"""

from __future__ import annotations

from typing import Any, Optional, Sequence

from asop.errors import Refusal

GATE_KINDS = ("deterministic", "judged", "human")

# The capability an L3-equivalent node declares to be routed judged gates.
# One string in one place, because a capability spelled two ways is a lane
# that silently has no workers.
VERIFY_CAPABILITY = "verify"

# What happens to a gated item still unverified when its park clock expires.
# `escalate` is the only value that needs a destination, because it is the
# only one that hands the decision to somebody.
ON_TIMEOUT = ("pass", "fail", "escalate")

CORE_FIELDS = ("kind", "class", "check", "checks")
CLOCK_FIELDS = ("max_park_seconds", "on_timeout", "escalate_to", "verifier")
# The two clock fields whose presence, together, decides whether the group is
# "declared" at all. `escalate_to`/`verifier` are conditional WITHIN the
# group (by `on_timeout` and by `kind`), not part of deciding whether the
# group exists.
CLOCK_CORE_FIELDS = ("max_park_seconds", "on_timeout")
EXECUTION_FIELDS = ("cwd", "timeout_s", "rubric", "judge_route")
GATE_FIELDS = CORE_FIELDS + CLOCK_FIELDS + EXECUTION_FIELDS

ATTESTATION_FIELDS = ("check", "exit_status", "environment", "at", "submitted_by")

SCHEMA_VERSION = 1

GATE_INVALID = "gate_invalid"
ATTESTATION_INVALID = "attestation_invalid"
ATTESTATION_REQUIRED = "attestation_required"


def _refuse(code: str, message: str, remediation: str) -> None:
    """Every refusal here carries a remediation, per `agentco/errors.py`.

    Not decoration: the caller of a gate boundary is usually an agent that
    will either fix the payload or stop calling, and which one it does
    depends entirely on whether the refusal named the next action.
    """
    raise Refusal(code=code, message=message, remediation=remediation)


def validate_gate(
    payload: Any,
    *,
    require: Sequence[str] = (),
    max_park_seconds_ceiling: Optional[int] = None,
) -> dict:
    """Return the normalised gate, or refuse. Never returns a partial gate.

    Normalised means every known field present and typed (`None` where
    absent), so no reader downstream has to decide what an absent field
    meant. A gate that reaches storage has already had every question about
    it answered.

    `require` names which optional groups a caller demands be fully present.
    The only group name understood today is `"clock"` — the plane passes
    `("clock",)`, meaning every gate it stores must declare its park clock;
    a caller with no park-clock concept passes `()`. Either way, the clock
    group is never accepted HALF-declared: see the module docstring.
    """
    known = f"the known fields are {list(GATE_FIELDS)}"
    if not isinstance(payload, dict):
        _refuse(
            GATE_INVALID,
            f"a gate must be an object, got {type(payload).__name__}",
            f"Send a gate object: {known}.",
        )

    # A gate this function already normalised carries `schema_version`, and
    # a store re-validates on every write. Its own output is not an unknown
    # field; a DIFFERENT schema version is a different contract and refused.
    declared = payload.get("schema_version")
    if declared is not None and declared != SCHEMA_VERSION:
        _refuse(
            GATE_INVALID,
            f"gate declares schema_version={declared!r}; this build speaks {SCHEMA_VERSION}",
            "Regenerate the gate with the current contract, or upgrade the reader.",
        )
    # `None` is absent. The normalised output carries every known field,
    # `None` where the author wrote nothing, and a store re-validates on
    # every write — so presence must mean "has a value", or the contract
    # refuses its own output the first time anything else on the bead moves.
    payload = {k: v for k, v in payload.items() if v is not None}
    unknown = sorted(set(payload) - set(GATE_FIELDS) - {"schema_version"})
    if unknown:
        _refuse(
            GATE_INVALID,
            f"unknown gate field(s) {unknown}",
            f"Remove or correct them — {known}. A gate with a misspelled "
            f"field is a check that never runs and still reports green, "
            f"which is why this is refused rather than ignored.",
        )

    kind = _normalise_kind(payload)
    check, checks = _normalise_check(payload)

    clock = _validate_clock(
        payload,
        kind=kind,
        required="clock" in require,
        ceiling=max_park_seconds_ceiling,
    )
    execution = _validate_execution(payload, kind=kind)

    return {
        "schema_version": SCHEMA_VERSION,
        "kind": kind,
        "check": check,
        "checks": checks,
        **clock,
        **execution,
    }


def _normalise_kind(payload: dict) -> str:
    """Resolve `kind`, accepting `class` as a read alias for it.

    A caller sending both is refused if they disagree — silently preferring
    one would mean the payload the author wrote and the gate that got stored
    disagree about something a reader needs to trust.
    """
    kind = payload.get("kind")
    cls = payload.get("class")
    if kind not in (None, "") and cls not in (None, "") and kind != cls:
        _refuse(
            GATE_INVALID,
            f"gate carries both 'kind' ({kind!r}) and 'class' ({cls!r}) and "
            f"they disagree",
            "'class' is a read alias for 'kind' — send one. Two names for "
            "the same field disagreeing is a gate that means something "
            "different depending on which reader you ask.",
        )
    resolved = kind if kind not in (None, "") else cls
    if resolved not in GATE_KINDS:
        _refuse(
            GATE_INVALID,
            f"gate kind must be one of {list(GATE_KINDS)}, got {resolved!r}",
            "Use 'deterministic' for a check the completing process re-runs, "
            "'judged' for one that needs a route other than the executor's, "
            "or 'human' for one that goes to a person.",
        )
    return resolved


def _normalise_check(payload: dict) -> tuple[Optional[str], Optional[list[str]]]:
    """Resolve `check`/`checks` to `(check, checks)`, exactly one populated.

    A one-element `checks` normalises INTO `check` — a staged gate of one
    stage is a gate with one check, and a reader should not have to branch
    on list-of-one versus scalar to learn that. Two or more stages stay a
    `checks` list; the caller re-runs them in order and stops at the first
    failure, but that execution behaviour belongs to whoever runs the check,
    not to this validation.
    """
    has_check = "check" in payload
    has_checks = "checks" in payload
    if has_check and has_checks:
        _refuse(
            GATE_INVALID,
            "gate carries both 'check' and 'checks' — they are mutually "
            "exclusive",
            "Use 'check' for one command, or 'checks' for an ordered list "
            "of stages. A gate declaring both looks staged while only one "
            "of the two would ever be read.",
        )
    if not has_check and not has_checks:
        _refuse(
            GATE_INVALID,
            "gate has neither 'check' nor 'checks'",
            "Give the command to re-run for a deterministic gate, or the "
            "criteria to apply for a judged or human one — as 'check' for "
            "one command, or 'checks' for an ordered list of stages. A gate "
            "with nothing to check is a status field.",
        )

    if has_checks:
        stages = payload["checks"]
        if isinstance(stages, str):
            _refuse(
                GATE_INVALID,
                f"'checks' must be a LIST of command strings, got the "
                f"string {stages!r}",
                f"Pass [{stages!r}] for one stage, or use 'check'. A bare "
                f"string is iterable, so it would otherwise validate "
                f"character-by-character into a gate of one-letter checks.",
            )
        if not isinstance(stages, (list, tuple)) or not stages:
            _refuse(
                GATE_INVALID,
                f"'checks' must be a non-empty list of command strings, "
                f"got {stages!r}",
                "A gate with no stages passes everything, which is not a "
                "gate. Give at least one stage, or use 'check'.",
            )
        for i, stage in enumerate(stages):
            if not isinstance(stage, str) or not stage.strip():
                _refuse(
                    GATE_INVALID,
                    f"'checks'[{i}] must be a non-empty string, got {stage!r}",
                    "Every stage needs a command or criterion to check.",
                )
        cleaned = [s.strip() for s in stages]
        if len(cleaned) == 1:
            return cleaned[0], None
        return None, cleaned

    check = payload["check"]
    if not isinstance(check, str) or not check.strip():
        _refuse(
            GATE_INVALID,
            "gate check must be a non-empty string",
            "Give the command to re-run for a deterministic gate, or the "
            "criteria to apply for a judged or human one. A gate with "
            "nothing to check is a status field.",
        )
    return check.strip(), None


def _validate_clock(payload: dict, *, kind: str, required: bool, ceiling: Optional[int]) -> dict:
    """Validate the park-clock group: `max_park_seconds`, `on_timeout`,
    `escalate_to`, `verifier`. Declared as a whole or not at all.

    `verifier` and `escalate_to` answer different questions, so they are
    different fields. `verifier` is who is supposed to answer the gate;
    `escalate_to` is where the decision goes when the clock runs out and
    nobody did. They are often the same person and never the same question —
    conflating them is how a human gate that resolved on its own clock got
    routed to nobody at all.
    """
    present = {f: payload.get(f) for f in CLOCK_FIELDS if f in payload and payload[f] not in (None, "")}
    core_present = [f for f in CLOCK_CORE_FIELDS if f in present]

    if not present:
        if required:
            _refuse(
                GATE_INVALID,
                f"missing required gate field(s) {list(CLOCK_CORE_FIELDS)}",
                f"Declare all of {list(CLOCK_CORE_FIELDS)}. Every one of "
                f"them is a question somebody has to answer about this "
                f"check, and the plane is not entitled to answer any of "
                f"them on your behalf.",
            )
        return {"max_park_seconds": None, "on_timeout": None, "escalate_to": None, "verifier": None}

    if len(core_present) != len(CLOCK_CORE_FIELDS):
        missing = [f for f in CLOCK_CORE_FIELDS if f not in core_present]
        _refuse(
            GATE_INVALID,
            f"gate declares part of its park clock ({sorted(present)}) but "
            f"not all of {list(CLOCK_CORE_FIELDS)} (missing {missing})",
            "Declare the clock or don't — a half-declared park clock parks "
            "forever by omission rather than by anyone's decision. Set "
            f"both {list(CLOCK_CORE_FIELDS)}, or drop every clock field.",
        )

    park = payload["max_park_seconds"]
    # `bool` is an `int` in Python, and `max_park_seconds: True` would
    # otherwise normalise into a one-second window.
    if isinstance(park, bool) or not isinstance(park, int) or park <= 0:
        _refuse(
            GATE_INVALID,
            f"max_park_seconds must be a positive integer, got {park!r}",
            "Declare how long this gate may stay unverified, in seconds, "
            "before its on_timeout resolution applies.",
        )
    if ceiling is not None and park > ceiling:
        _refuse(
            GATE_INVALID,
            f"max_park_seconds {park} exceeds the ceiling {ceiling}",
            f"Use at most {ceiling} seconds. A window longer than that is "
            f"parking forever with extra steps, and parking forever is the "
            f"state the clock exists to prevent.",
        )

    on_timeout = payload["on_timeout"]
    if on_timeout not in ON_TIMEOUT:
        _refuse(
            GATE_INVALID,
            f"on_timeout must be one of {list(ON_TIMEOUT)}, got {on_timeout!r}",
            "Decide what happens to this unit of work if nobody verifies "
            "it in time: 'pass', 'fail', or 'escalate' to a named "
            "destination. There is deliberately no default — a "
            "library-level guess here silently passes or silently fails "
            "somebody else's release.",
        )

    escalate_to = payload.get("escalate_to")
    if on_timeout == "escalate" and not (isinstance(escalate_to, str) and escalate_to.strip()):
        _refuse(
            GATE_INVALID,
            "on_timeout='escalate' without escalate_to",
            "Name the destination the escalation goes to. An escalation "
            "with nowhere to go is indistinguishable from parking forever.",
        )
    if on_timeout != "escalate" and escalate_to not in (None, ""):
        _refuse(
            GATE_INVALID,
            f"escalate_to is set but on_timeout is {on_timeout!r}, so "
            f"nothing would ever read it",
            "Either set on_timeout='escalate', or drop escalate_to. Two "
            "readers of this gate would otherwise disagree about what it "
            "means.",
        )

    verifier = payload.get("verifier")
    named = isinstance(verifier, str) and bool(verifier.strip())
    if kind == "human" and not named:
        _refuse(
            GATE_INVALID,
            "a human gate must name the person who answers it (verifier)",
            "Set verifier to whoever is expected to sign this off. Without "
            "it the routed work item has no assignee and requires no "
            "capability, so the queue offers it to the executor — which is "
            "the one party a human gate exists to exclude. escalate_to is "
            "not a substitute: it names where the decision goes when "
            "nobody answers, and it cannot even be declared unless "
            "on_timeout is 'escalate'.",
        )
    if kind == "deterministic" and verifier not in (None, ""):
        _refuse(
            GATE_INVALID,
            "verifier is set on a deterministic gate, so nothing would "
            "ever read it",
            "Drop it. A deterministic check is re-run by the process that "
            "completed the work, and that process is its attester — "
            "naming somebody else here promises a review that never "
            "happens.",
        )

    return {
        "max_park_seconds": park,
        "on_timeout": on_timeout,
        "escalate_to": escalate_to.strip() if isinstance(escalate_to, str) else None,
        "verifier": verifier.strip() if named else None,
    }


def _validate_execution(payload: dict, *, kind: str) -> dict:
    """Validate the runtime-hint group: `cwd`, `timeout_s`, `rubric`,
    `judge_route`. Always optional; each field's VALIDITY still depends on
    `kind`, the same way `verifier` does on the clock side.
    """
    cwd = payload.get("cwd")
    if cwd is not None:
        if not isinstance(cwd, str) or not cwd.strip():
            _refuse(
                GATE_INVALID,
                f"cwd must be a non-empty string, got {cwd!r}",
                "Give the directory the check runs from, or omit the key.",
            )
        cwd = cwd.strip()

    timeout_s = payload.get("timeout_s")
    if timeout_s is not None:
        # bool is an int subclass; a True timeout is a bug, not a duration.
        if isinstance(timeout_s, bool) or not isinstance(timeout_s, int) or timeout_s <= 0:
            _refuse(
                GATE_INVALID,
                f"timeout_s must be a positive integer (seconds), got {timeout_s!r}",
                "Give the per-command budget in seconds, or omit the key.",
            )

    rubric = payload.get("rubric")
    if rubric is not None:
        if not isinstance(rubric, str) or not rubric.strip():
            _refuse(
                GATE_INVALID,
                f"rubric must be a non-empty string, got {rubric!r}",
                "Give the criteria a judge weighs, or omit the key.",
            )
        if kind != "judged":
            _refuse(
                GATE_INVALID,
                f"rubric is set on a {kind!r} gate, so nothing would ever read it",
                "Drop it, or change kind to 'judged'. A deterministic check "
                "is re-run by the process that completed the work; a human "
                "gate is answered by the named verifier's own judgement. "
                "Neither reads a rubric.",
            )
        rubric = rubric.strip()

    judge_route = payload.get("judge_route")
    if judge_route is not None:
        if not isinstance(judge_route, str) or not judge_route.strip():
            _refuse(
                GATE_INVALID,
                f"judge_route must be a non-empty string, got {judge_route!r}",
                "Give the route the judgment happens on, or omit the key.",
            )
        if kind != "judged":
            _refuse(
                GATE_INVALID,
                f"judge_route is set on a {kind!r} gate, so nothing would "
                f"ever read it",
                "Drop it, or change kind to 'judged'. Only a judged gate "
                "is routed to a judge at all.",
            )
        judge_route = judge_route.strip()

    return {"cwd": cwd, "timeout_s": timeout_s, "rubric": rubric, "judge_route": judge_route}


def validate_attestation(payload: Any, *, gate: dict, submitted_by: str) -> dict:
    """Return the normalised attestation, or refuse.

    The plane checks that the record is well formed and that it attests to
    **this gate's** check. An attestation naming a different command is not
    evidence about this unit of work, and accepting one is how a green
    report gets produced by running something easier.

    `submitted_by` comes from the authenticated actor, never from the
    payload — the same rule as everywhere else in this contract. A body that
    tries to say otherwise is refused rather than believed, because an
    attestation is a claim about who ran what, and a claim whose author can
    be forged is not evidence.
    """
    if not isinstance(payload, dict):
        _refuse(
            ATTESTATION_INVALID,
            f"an attestation must be an object, got {type(payload).__name__}",
            f"Send an attestation object with {list(ATTESTATION_FIELDS)}.",
        )

    unknown = sorted(set(payload) - set(ATTESTATION_FIELDS))
    if unknown:
        _refuse(
            ATTESTATION_INVALID,
            f"unknown attestation field(s) {unknown}",
            f"The record's fields are {list(ATTESTATION_FIELDS)}. Anything "
            f"else belongs in the work item's result, not in the evidence.",
        )

    if "submitted_by" in payload and payload["submitted_by"] != submitted_by:
        _refuse(
            ATTESTATION_INVALID,
            f"submitted_by in the body does not match the authenticated "
            f"actor ({submitted_by!r})",
            "Omit submitted_by — it is derived from the signature. The "
            "plane records who actually called it, not who the call says "
            "it is.",
        )

    check = payload.get("check")
    if not isinstance(check, str) or not check.strip():
        _refuse(
            ATTESTATION_INVALID,
            "attestation check must name the command that was run",
            "Set check to the command you re-ran, exactly as the gate "
            "declares it.",
        )
    gate_check = gate.get("check")
    if check.strip() != gate_check:
        _refuse(
            ATTESTATION_INVALID,
            f"the attestation is about {check.strip()!r}, but this item's "
            f"gate checks {gate_check!r}",
            f"Run {gate_check!r} and attest to that. Evidence about a "
            f"different command is not evidence about this item.",
        )

    exit_status = payload.get("exit_status")
    if isinstance(exit_status, bool) or not isinstance(exit_status, int):
        _refuse(
            ATTESTATION_INVALID,
            f"exit_status must be an integer, got {exit_status!r}",
            "Report the process's actual exit status. A boolean here makes "
            "'it passed' an opinion rather than an observation.",
        )

    environment = payload.get("environment")
    if not isinstance(environment, str) or not environment.strip():
        _refuse(
            ATTESTATION_INVALID,
            "environment must fingerprint where the check ran",
            "Include enough to identify the machine, image or runtime — an "
            "attestation nobody can reproduce on cannot be disputed either.",
        )

    at = payload.get("at")
    if not isinstance(at, str) or not at.strip():
        _refuse(
            ATTESTATION_INVALID,
            "at must be the timestamp of the run",
            "Set at to when the check ran, ISO-8601. Without it, a stale "
            "attestation is indistinguishable from a fresh one.",
        )

    return {
        "check": check.strip(),
        "exit_status": exit_status,
        "environment": environment.strip(),
        "at": at.strip(),
        "submitted_by": submitted_by,
    }


def attestation_passes(attestation: dict) -> bool:
    """Exit 0 and nothing else. The one place this convention is stated."""
    return attestation.get("exit_status") == 0


def retry_decision(failures: int) -> str:
    """What to do after `failures` failed verifications of the same unit.

    `fix` once, then `escalate`, then never again autonomously. The third
    attempt is the one that matters: an agent that keeps regenerating a fix
    for a check it does not understand burns budget and buries the signal,
    and the policy that stops it has to be a rule rather than a hope.
    Returning a decision rather than acting on it keeps the caller out of
    the business of spawning work as a side effect of a report.
    """
    if failures <= 0:
        raise ValueError("retry_decision applies after a failure, got 0")
    if failures == 1:
        return "fix"
    if failures == 2:
        return "escalate"
    return "stop"
