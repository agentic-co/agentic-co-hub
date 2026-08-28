"""Refusals that carry a machine code AND a remediation sentence.

The rule: "Never a submission that returns success and produces
nothing. Every refusal carries a machine code and a remediation sentence
generated from the command registry."

The remediation is not decoration. the scope-model decision's whole argument is that a
`ScopeLease` registry whose leases all intersect becomes noise within four
days; the thing that stops a colleague concluding the tool is broken when
their first `POST` is refused is a sentence that names what to do instead.
A refusal that says only `scope_too_broad` teaches them to stop calling.

`Refusal` is deliberately NOT an HTTPException subclass — this module is
imported by the CLI and the digest job as well as the app, and none of them
should have to depend on FastAPI to read a refusal. `app.py` owns the one
translation into HTTP.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Refusal(Exception):
    """A request the registry declines, with the reason machine- and human-readable.

    `code` is stable and greppable (clients branch on it). `remediation` is a
    complete sentence addressed to the caller. `http_status` is carried here
    rather than mapped in the app so that a new refusal cannot be added
    without its author deciding what it means over HTTP.
    """

    code: str
    message: str
    remediation: str
    http_status: int = 422

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.code}: {self.message} — {self.remediation}"

    def to_dict(self) -> dict:
        return {
            "state": "refused",
            "code": self.code,
            "message": self.message,
            "remediation": self.remediation,
        }


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
