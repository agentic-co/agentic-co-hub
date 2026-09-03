"""`Refusal` and the plane's own refusal shapes.

`Refusal` itself has moved to `asop.errors` — the shared ASOP contract's one
exception type, so that a caller validating a gate or an attestation against
`asop.gates` never has to know whether it is talking to this plane or to a
harness that only depends on `asop`. This module re-exports it rather than
redefining it: `agentco.errors.Refusal is asop.errors.Refusal` holds, and
that identity is the point — two `Refusal` classes would mean a `try/except
Refusal` written against one side silently missing refusals raised by the
other. See `packages/asop/asop/errors.py` for the type itself.

What stays here are the refusal shapes that are NOT part of the shared
contract because nothing outside this plane has an opinion on them:
`Unauthenticated` is an HTTP-transport concern (this plane's auth), and
`scope_too_broad` is a `ScopeClaim` concern (this plane's concurrency
primitive — a harness adopting the ASOP contract has no reason to know what
a scope claim is).
"""

from __future__ import annotations

from asop.errors import Refusal

__all__ = ["Refusal", "Unauthenticated", "scope_too_broad"]


class Unauthenticated(Refusal):
    """Separate type so the app can 401 without string-matching a code."""


def scope_too_broad(prefix: str, min_segments: int) -> Refusal:
    """the scope-model decision's headline refusal: the one that keeps the registry precise.

    A lease on `src/` or on the repo root is the DEFAULT outcome if nothing
    refuses it, and it is the outcome that makes every claim intersect every
    other. The remediation names the requirement rather than restating the
    rule, because the caller's next action is to re-POST with a deeper path.
    """
    depth = len([s for s in prefix.strip("/").split("/") if s]) if prefix.strip("/") else 0
    return Refusal(
        code="scope_too_broad",
        message=(
            f"path prefix {prefix!r} names {depth} segment(s) below the repo root; "
            f"the minimum is {min_segments}"
        ),
        remediation=(
            f"Re-claim naming at least {min_segments} directory segments — e.g. "
            f"'src/budget/grid' rather than 'src/'. A lease at repo-root or "
            f"single-segment depth intersects every other lease, which is what "
            f"makes a scope registry stop being read."
        ),
    )
