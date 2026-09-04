"""asop — the Agentic Standard Operating Procedure contract.

The shared shape that any coordination plane and any harness executing its
work can both validate against, without either depending on the other. See
`README.md` for what belongs here versus what stays plane- or harness-side.

`SCHEMA_VERSION` covers the gate schema (`asop.gates.validate_gate`'s
normalised output carries it as `schema_version`). It changes only for a
breaking change to that normalised shape — a new required field, a renamed
one, a changed type — never for an additive optional field or a new refusal
code, both of which existing readers already tolerate. Bump it in the same
change that breaks compatibility, never after the fact.
"""

from __future__ import annotations

from asop.errors import Refusal
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
    validate_gate,
)
from asop.sop import (
    ASOP,
    ASOP_VERSION,
    DEFAULT_PROTECTED_TAGS,
    MAX_DEPTH,
    MAX_STEPS,
    ROLE_KINDS,
    SOP,
    SopContractError,
    SopError,
    SopStatus,
    Step,
    validate_asop,
    validate_fields,
    validate_step,
)

SCHEMA_VERSION = 1

__all__ = [
    "SCHEMA_VERSION",
    "Refusal",
    "GATE_KINDS",
    "GATE_FIELDS",
    "ATTESTATION_FIELDS",
    "ON_TIMEOUT",
    "VERIFY_CAPABILITY",
    "GATE_INVALID",
    "ATTESTATION_INVALID",
    "ATTESTATION_REQUIRED",
    "validate_gate",
    "validate_attestation",
    "attestation_passes",
    "retry_decision",
    "ASOP",
    "ASOP_VERSION",
    "DEFAULT_PROTECTED_TAGS",
    "Step",
    "MAX_STEPS",
    "MAX_DEPTH",
    "ROLE_KINDS",
    "validate_asop",
    "validate_step",
    "SOP",
    "SopStatus",
    "SopError",
    "SopContractError",
    "validate_fields",
]
