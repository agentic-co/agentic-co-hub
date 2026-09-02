"""One classification of the library's own exceptions, for every transport.

`WorkError`, `SopError` and their kin predate `Refusal` and carry no code. The
HTTP surface gave each a stable code and status so a client could branch on it;
the MCP surface forwarded the bare message. The same wrong input therefore came
back as `work_conflict` on one transport and as prose on another — a difference
the conformance suite would name as a bug, and did. This module is the single
mapping, imported by both surfaces, so a refusal reads the same wherever it is
read. Nothing here decides anything; it names what the library already decided.
"""

from __future__ import annotations

from agentco.errors import Refusal
from agentco.keys import NaturalKeyError
from agentco.policy import RevisionPolicyError
from agentco.sop import SopError
from agentco.work import CapabilityError, DecompositionError, WorkError


def classify(exc: Exception) -> Refusal:
    """A `Refusal` with a stable code for any exception the core raises.

    A `Refusal` passes through untouched. Order matters below: the specific
    classes are checked before their bases, and `RevisionPolicyError` is a
    `ValueError` rather than a `SopError`, so it is checked before both.

    Nothing may fall through to a generic 500. A fenced report arriving late is
    the queue working as designed; reporting it as a server bug would teach a
    worker its correct behaviour is a crash.
    """
    if isinstance(exc, Refusal):
        return exc
    if isinstance(exc, NaturalKeyError):
        return Refusal(code="natural_key_invalid", message=str(exc), remediation=str(exc), http_status=400)
    if isinstance(exc, CapabilityError):
        return Refusal(code="capability_mismatch", message=str(exc), remediation=str(exc), http_status=409)
    if isinstance(exc, DecompositionError):
        # 422: the body named a tree position this item may not take. Not a
        # conflict with the state of the world — the bound was known.
        return Refusal(code="decomposition_bound", message=str(exc), remediation=str(exc), http_status=422)
    if isinstance(exc, WorkError):
        # LeaseError and BlockedError both land here. 409 rather than 422: the
        # request was well-formed and lost to the state of the world.
        return Refusal(code="work_conflict", message=str(exc), remediation=str(exc), http_status=409)
    if isinstance(exc, RevisionPolicyError):
        # 403, not 422: the body was well-formed and the actor authenticated.
        # What was refused is who asked. `rule` names which of the three.
        return Refusal(code=f"revision_policy:{exc.rule}", message=str(exc), remediation=str(exc), http_status=403)
    if isinstance(exc, SopError):
        return Refusal(code="sop_refused", message=str(exc), remediation=str(exc), http_status=422)
    return Refusal(code="invalid_request", message=str(exc), remediation=str(exc), http_status=400)
