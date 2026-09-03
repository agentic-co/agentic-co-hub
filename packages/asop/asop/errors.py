"""`Refusal` — the one exception type the ASOP contract speaks in.

Moved verbatim out of `agentco/errors.py`. This is the load-bearing type: any
plane or harness that validates a gate, an attestation, or an SOP record
against this package raises (or catches) exactly this, so a caller never has
to know which side of the contract — plane or executor — produced the
refusal. `agentco.errors.Refusal` re-exports this class rather than defining
its own, and `agentco.errors.Refusal is asop.errors.Refusal` is the identity
that proves it: two `Refusal` types would mean a `try/except Refusal` written
against one side silently missing refusals raised by the other.

Plane-specific refusal shapes — `Unauthenticated` (an HTTP-transport
concern), `scope_too_broad` (a `ScopeClaim` concern) — stay in
`agentco/errors.py`. Nothing about authentication or directory-scope claims
is part of the ASOP contract; only the gate/attestation/SOP shapes are.
"""

from __future__ import annotations

from dataclasses import dataclass


# NOT frozen, and that is deliberate rather than an oversight. A frozen
# dataclass subclassing Exception cannot have `__traceback__` assigned —
# `FrozenInstanceError` — which breaks any library that rewrites tracebacks,
# and pytest and multiprocessing both do. Immutability is not worth that: an
# exception is thrown and read, and nothing here mutates one.
@dataclass
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

    def __post_init__(self) -> None:
        """Populate `args`, which is what makes this exception survive a pickle.

        `Exception.__reduce__` reconstructs from `(cls, self.args)`, and a
        dataclass exception never populates `args` on its own — so a Refusal
        crossing a process boundary came back as
        `TypeError: __init__() missing 3 required positional arguments`, having
        lost its code, message and remediation on the way.
        """
        super().__init__(self.code, self.message, self.remediation)

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.code}: {self.message} — {self.remediation}"

    def to_dict(self) -> dict:
        return {
            "state": "refused",
            "code": self.code,
            "message": self.message,
            "remediation": self.remediation,
        }
