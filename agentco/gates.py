"""Verification gates — the `verified` property the ASOP contract rests on.

A procedure that says what done means, and cannot tell whether done happened,
is a document. The gate is the difference: a declared check, attached to a unit
of work, that must produce evidence before the unit counts as complete.

Three kinds, and the difference between them is *who is allowed to run the
check* (`docs/decisions/0002-participation-ladder.md`):

  `deterministic` — re-run fresh by the completing process, which submits an
      **attestation**. The plane verifies the record's SHAPE and stores the
      claim. It never runs the command. That is a trust floor, not a proof: a
      sloppy or compromised executor can report exit 0 on a check it never
      ran, and L3 is what closes that. Every document describing attestation
      has to say so, this one included.
  `judged` — needs a route different from the executor's, so it becomes a
      verify work item claimable only by a worker declaring the `verify`
      capability.
  `human` — goes to the routing spine, addressed to the person named in
      `verifier`.

**`verifier` and `escalate_to` answer different questions, so they are
different fields.** `verifier` is who is supposed to answer the gate;
`escalate_to` is where the decision goes when the clock runs out and nobody
did. They are often the same person and they are never the same question — and
L3 conflated them, which is how a human gate that resolved on its own clock got
routed to nobody at all: `escalate_to` is refused unless `on_timeout` is
`escalate`, so a human gate declaring `pass` or `fail` produced a work item
with no assignee and no required capability, offered to the executor by
`ready()`. A human gate therefore MUST name a verifier. A `deterministic` gate
must not: its executor is its attester, and a name there would be a field
nothing reads. A `judged` gate may, which narrows the route from "any node
declaring `verify`" to one of them.

**A malformed gate is refused at the write boundary, never stored.** This is
the whole reason validation lives here rather than at the point of use: a gate
that quietly does nothing is worse than no gate at all, because it reports
green. So an unknown key is a refusal rather than a field nobody reads — the
usual forward-compatibility argument (`WorkItem.unknown`) is about a field a
NEWER writer added, while an unknown key in a gate is overwhelmingly a typo,
and the cost of guessing wrong is a check that never runs and nobody misses.

**Every gate declares its own timeout resolution.** There is no library-wide
default for `on_timeout`, and that omission is deliberate: what should happen
to a unit of work nobody verified within the window is a judgement about that
work, and a plane-level default would silently pass or silently fail somebody
else's release. Declaring it is one line; guessing it is an outage.

The clock itself is not enforced here — parking, escalation and resolution on
the deadline are the L3 verifier's, and land with it. What Phase 1 owes is that
the declaration is present, validated and stored from the first gate onward,
so that turning the clock on later is not a migration of every gate ever
written.
"""

from __future__ import annotations

from typing import Any, Optional

from agentco.errors import Refusal

GATE_KINDS = ("deterministic", "judged", "human")

# The capability an L3 node declares to be routed judged gates. It lives here
# rather than in `agentco/verifiers.py` so that `work.py` can enforce it without
# importing the module that imports `work.py` — and one string in one place,
# because a capability spelled two ways is a lane that silently has no workers.
VERIFY_CAPABILITY = "verify"

# What happens to a gated item still unverified when its park clock expires.
# `escalate` is the only value that needs a destination, because it is the only
# one that hands the decision to somebody.
ON_TIMEOUT = ("pass", "fail", "escalate")

GATE_FIELDS = ("kind", "check", "max_park_seconds", "on_timeout", "escalate_to", "verifier")
REQUIRED_GATE_FIELDS = ("kind", "check", "max_park_seconds", "on_timeout")

ATTESTATION_FIELDS = ("check", "exit_status", "environment", "at", "submitted_by")

# One park clock ceiling, so a gate cannot declare a window long enough to be
# indistinguishable from parking forever — which is the state the L3 phase
# exists to make impossible.
MAX_PARK_SECONDS = 30 * 24 * 3600


GATE_INVALID = "gate_invalid"
ATTESTATION_INVALID = "attestation_invalid"
ATTESTATION_REQUIRED = "attestation_required"


def _refuse(code: str, message: str, remediation: str) -> None:
    """Every refusal here carries a remediation, per `agentco/errors.py`.

    Not decoration: the caller of a gate boundary is usually an agent that
    will either fix the payload or stop calling, and which one it does depends
    entirely on whether the refusal named the next action.
    """
    raise Refusal(code=code, message=message, remediation=remediation)


def validate_gate(payload: Any) -> dict:
    """Return the normalised gate, or refuse. Never returns a partial gate.

    Normalised means every declared field present and typed, so that no reader
    downstream has to decide what an absent `on_timeout` meant. A gate that
    reaches storage has already had every question about it answered.
    """
    known = f"the known fields are {list(GATE_FIELDS)}"
    if not isinstance(payload, dict):
        _refuse(
            GATE_INVALID,
            f"a gate must be an object, got {type(payload).__name__}",
            f"Send a gate object: {known}.",
        )

    unknown = sorted(set(payload) - set(GATE_FIELDS))
    if unknown:
        _refuse(
            GATE_INVALID,
            f"unknown gate field(s) {unknown}",
            f"Remove or correct them — {known}. A gate with a misspelled field "
            f"is a check that never runs and still reports green, which is why "
            f"this is refused rather than ignored.",
        )

    missing = [f for f in REQUIRED_GATE_FIELDS if payload.get(f) in (None, "")]
    if missing:
        _refuse(
            GATE_INVALID,
            f"missing required gate field(s) {missing}",
            f"Declare all of {list(REQUIRED_GATE_FIELDS)}. Every one of them is "
            f"a question somebody has to answer about this check, and the plane "
            f"is not entitled to answer any of them on your behalf.",
        )

    kind = payload["kind"]
    if kind not in GATE_KINDS:
        _refuse(
            GATE_INVALID,
            f"gate kind must be one of {list(GATE_KINDS)}, got {kind!r}",
            "Use 'deterministic' for a check the completing process re-runs, "
            "'judged' for one that needs a route other than the executor's, or "
            "'human' for one that goes to a person.",
        )

    check = payload["check"]
    if not isinstance(check, str) or not check.strip():
        _refuse(
            GATE_INVALID,
            "gate check must be a non-empty string",
            "Give the command to re-run for a deterministic gate, or the "
            "criteria to apply for a judged or human one. A gate with nothing "
            "to check is a status field.",
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
    if park > MAX_PARK_SECONDS:
        _refuse(
            GATE_INVALID,
            f"max_park_seconds {park} exceeds the ceiling {MAX_PARK_SECONDS}",
            f"Use at most {MAX_PARK_SECONDS} seconds (30 days). A window "
            f"longer than that is parking forever with extra steps, and "
            f"parking forever is the state the clock exists to prevent.",
        )

    on_timeout = payload["on_timeout"]
    if on_timeout not in ON_TIMEOUT:
        _refuse(
            GATE_INVALID,
            f"on_timeout must be one of {list(ON_TIMEOUT)}, got {on_timeout!r}",
            "Decide what happens to this unit of work if nobody verifies it in "
            "time: 'pass', 'fail', or 'escalate' to a named destination. There "
            "is deliberately no default — a plane-level guess here silently "
            "passes or silently fails somebody else's release.",
        )

    escalate_to = payload.get("escalate_to")
    if on_timeout == "escalate" and not (isinstance(escalate_to, str) and escalate_to.strip()):
        _refuse(
            GATE_INVALID,
            "on_timeout='escalate' without escalate_to",
            "Name the destination the escalation goes to. An escalation with "
            "nowhere to go is indistinguishable from parking forever.",
        )
    if on_timeout != "escalate" and escalate_to not in (None, ""):
        _refuse(
            GATE_INVALID,
            f"escalate_to is set but on_timeout is {on_timeout!r}, so nothing "
            f"would ever read it",
            "Either set on_timeout='escalate', or drop escalate_to. Two "
            "readers of this gate would otherwise disagree about what it means.",
        )

    verifier = payload.get("verifier")
    named = isinstance(verifier, str) and verifier.strip()
    if kind == "human" and not named:
        _refuse(
            GATE_INVALID,
            "a human gate must name the person who answers it (verifier)",
            "Set verifier to whoever is expected to sign this off. Without it "
            "the routed work item has no assignee and requires no capability, "
            "so the queue offers it to the executor — which is the one party a "
            "human gate exists to exclude. escalate_to is not a substitute: it "
            "names where the decision goes when nobody answers, and it cannot "
            "even be declared unless on_timeout is 'escalate'.",
        )
    if kind == "deterministic" and verifier not in (None, ""):
        _refuse(
            GATE_INVALID,
            "verifier is set on a deterministic gate, so nothing would ever read it",
            "Drop it. A deterministic check is re-run by the process that "
            "completed the work, and that process is its attester — naming "
            "somebody else here promises a review that never happens.",
        )

    return {
        "kind": kind,
        "check": check.strip(),
        "max_park_seconds": park,
        "on_timeout": on_timeout,
        "escalate_to": escalate_to.strip() if isinstance(escalate_to, str) else None,
        "verifier": verifier.strip() if named else None,
    }


def validate_attestation(payload: Any, *, gate: dict, submitted_by: str) -> dict:
    """Return the normalised attestation, or refuse.

    The plane checks that the record is well formed and that it attests to
    **this gate's** check. An attestation naming a different command is not
    evidence about this unit of work, and accepting one is how a green report
    gets produced by running something easier.

    `submitted_by` comes from the authenticated actor, never from the payload —
    the same rule as everywhere else here. A body that tries to say otherwise
    is refused rather than believed, because an attestation is a claim about
    who ran what, and a claim whose author can be forged is not evidence.
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
            f"submitted_by in the body does not match the authenticated actor "
            f"({submitted_by!r})",
            "Omit submitted_by — it is derived from the signature. The plane "
            "records who actually called it, not who the call says it is.",
        )

    check = payload.get("check")
    if not isinstance(check, str) or not check.strip():
        _refuse(
            ATTESTATION_INVALID,
            "attestation check must name the command that was run",
            "Set check to the command you re-ran, exactly as the gate "
            "declares it.",
        )
    if check.strip() != gate["check"]:
        _refuse(
            ATTESTATION_INVALID,
            f"the attestation is about {check.strip()!r}, but this item's gate "
            f"checks {gate['check']!r}",
            f"Run {gate['check']!r} and attest to that. Evidence about a "
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
    attempt is the one that matters: an agent that keeps regenerating a fix for
    a check it does not understand burns budget and buries the signal, and the
    policy that stops it has to be a rule rather than a hope. Returning a
    decision rather than acting on it keeps the queue out of the business of
    spawning work as a side effect of a report.
    """
    if failures <= 0:
        raise ValueError("retry_decision applies after a failure, got 0")
    if failures == 1:
        return "fix"
    if failures == 2:
        return "escalate"
    return "stop"
