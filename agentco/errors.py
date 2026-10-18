"""`Refusal` and the plane's own refusal shapes.

`Refusal` itself has moved to `asop.errors` — the shared ASOP contract's one
exception type, so that a caller validating a gate or an attestation against
`asop.gates` never has to know whether it is talking to this plane or to a
harness that only depends on `asop`. This module re-exports it rather than
redefining it: `agentco.errors.Refusal is asop.errors.Refusal` holds, and
that identity is the point — two `Refusal` classes would mean a `try/except
Refusal` written against one side silently missing refusals raised by the
other. See `asop/errors.py` in https://github.com/mabidoli/asop for the type itself.

What stays here are the refusal shapes that are NOT part of the shared
contract because nothing outside this plane has an opinion on them:
`Unauthenticated` is an HTTP-transport concern (this plane's auth), and
`scope_too_broad` is a `ScopeClaim` concern (this plane's concurrency
primitive — a harness adopting the ASOP contract has no reason to know what
a scope claim is).
"""

from __future__ import annotations

from dataclasses import dataclass

from asop.errors import Refusal

__all__ = [
    "AliasedRefusal", "HUMAN_REQUIRED", "MANIFEST_UNKNOWN", "Refusal", "Unauthenticated",
    "human_required", "manifest_unknown", "scope_too_broad",
]

# --------------------------------------------------------------------------- #
# Codes this hub raises ahead of the spec release that catalogs them
#
# Owner decision 9 (2026-10-18). Spec 0.6.1 (unreleased when this was written)
# adds `human_required` — a non-human actor attempting a human-only verb — and
# keeps `capability_mismatch` for capability and role binding failures only;
# the hub also renames its `manifest_member_unknown` to `manifest_unknown` where
# the case is "the target manifest is not recorded". The strings live HERE, not
# in `asop.refusals`, so the hub does not depend on an unreleased contract;
# `tests/test_refusal_codes.py` fails if the installed contract catalogs them
# under different spellings.
# --------------------------------------------------------------------------- #

HUMAN_REQUIRED = "human_required"
MANIFEST_UNKNOWN = "manifest_unknown"


@dataclass
class AliasedRefusal(Refusal):
    """A refusal whose code was renamed, carrying the old code for one minor.

    `aliases` is DEPRECATED on arrival: a client that branched on the old code
    keeps working by reading it, and it is removed after one minor release
    (docs/wire.md, "Renamed refusal codes")."""

    aliases: tuple = ()

    def to_dict(self) -> dict:
        out = super().to_dict()
        if self.aliases:
            out["aliases"] = list(self.aliases)
        return out


def human_required(message: str, remediation: str, http_status: int = 403) -> AliasedRefusal:
    """A non-human actor attempted a human-only verb. Formerly `capability_mismatch`."""
    return AliasedRefusal(HUMAN_REQUIRED, message, remediation, http_status, aliases=("capability_mismatch",))


def manifest_unknown(message: str, remediation: str, http_status: int = 404) -> AliasedRefusal:
    """The manifest a request names is not recorded. Formerly `manifest_member_unknown`."""
    return AliasedRefusal(MANIFEST_UNKNOWN, message, remediation, http_status,
                          aliases=("manifest_member_unknown",))


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
