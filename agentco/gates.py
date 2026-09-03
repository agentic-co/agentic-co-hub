"""Verification gates — the `verified` property the ASOP contract rests on.

The gate schema itself — the `deterministic` / `judged` / `human` split, the
park-clock fields, and the attestation shape — has moved to `asop.gates`,
the package shared with any harness that speaks the same ASOP contract (the
AgentCo Harness, from its P2). This module is now a thin shim: it keeps this
plane's own numbers (`MAX_PARK_SECONDS` — a decision about THIS registry,
not a property of what a gate is) and calls `asop.gates.validate_gate` with
this plane's calling convention, so every existing caller in this repo is
unchanged.

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
did. A human gate therefore MUST name a verifier; a `deterministic` gate
must not (its executor is its attester); a `judged` gate may, narrowing the
route from "any node declaring `verify`" to one of them. See
`asop.gates.validate_gate`'s docstring for the reasoning in full.
"""

from __future__ import annotations

from typing import Any

from asop.gates import (
    ATTESTATION_FIELDS,
    ATTESTATION_INVALID,
    ATTESTATION_REQUIRED,
    GATE_FIELDS,
    GATE_INVALID,
    GATE_KINDS,
    ON_TIMEOUT,
    VERIFY_CAPABILITY,
    attestation_passes,
    retry_decision,
    validate_attestation,
)
from asop.gates import validate_gate as _validate_gate

__all__ = [
    "GATE_KINDS",
    "VERIFY_CAPABILITY",
    "ON_TIMEOUT",
    "GATE_FIELDS",
    "REQUIRED_GATE_FIELDS",
    "ATTESTATION_FIELDS",
    "MAX_PARK_SECONDS",
    "GATE_INVALID",
    "ATTESTATION_INVALID",
    "ATTESTATION_REQUIRED",
    "validate_gate",
    "validate_attestation",
    "attestation_passes",
    "retry_decision",
]

# This plane requires every gate to declare its park clock — carried here as
# the calling convention (`require=("clock",)` below), not as a property of
# the shared schema. Kept for anything that still inspects it directly.
REQUIRED_GATE_FIELDS = ("kind", "check", "max_park_seconds", "on_timeout")

# One park clock ceiling, so a gate cannot declare a window long enough to be
# indistinguishable from parking forever. This is THIS plane's decision about
# its own registry — `asop.gates.validate_gate` takes it as an argument
# rather than baking it in, because "thirty days" is not a property of what
# a gate is.
MAX_PARK_SECONDS = 30 * 24 * 3600


def validate_gate(payload: Any) -> dict:
    """Return the normalised gate, or refuse — this plane's calling
    convention over `asop.gates.validate_gate`: the park clock is required,
    and `MAX_PARK_SECONDS` is the ceiling.
    """
    return _validate_gate(payload, require=("clock",), max_park_seconds_ceiling=MAX_PARK_SECONDS)
