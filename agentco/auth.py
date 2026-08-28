"""HMAC request authentication — deliberately the boring option first.

OIDC is the eventual answer and the right one for an organisation that already
has an identity provider. It is not the FIRST answer, because standing up a
client registration usually needs an administrator's consent, and "wait for an
admin" is exactly the friction that stops a coordination tool being adopted at
all. A per-actor shared secret is one line of configuration and works
identically on every platform.

The signature covers method, path, timestamp and a digest of the body. A body
digest rather than the raw body so verification never holds a second copy of a
large payload; the timestamp inside the signed string so a captured request
cannot be replayed once the window closes.

**The actor identity comes from the token, never from the payload.** Any
`requestedBy`-style field a client sends is advisory. This matters more than it
looks: a client that could name another person as the holder of a scope claim
could file claims in their name and make it appear they were working somewhere
they were not.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from pathlib import Path
from typing import Mapping, Optional

from agentco.errors import Unauthenticated

ACTOR_HEADER = "x-agentco-actor"
TIMESTAMP_HEADER = "x-agentco-timestamp"
SIGNATURE_HEADER = "x-agentco-signature"

# Replay window. Five minutes each way absorbs ordinary clock skew between a
# laptop and the VM without giving a captured request a useful lifetime.
MAX_SKEW_S = 300

KEYS_ENV_VAR = "AGENTCO_REGISTRY_KEYS"


def _unauthenticated(message: str, remediation: str) -> Unauthenticated:
    return Unauthenticated(
        code="unauthenticated",
        message=message,
        remediation=remediation,
        http_status=401,
    )


def load_keys(path: str | Path | None = None) -> dict[str, str]:
    """actor → shared secret, from a JSON file.

    Precedence: explicit argument (tests) > `$AGENTCO_REGISTRY_KEYS` > nothing.
    "Nothing" is an empty mapping and therefore refuses every request — a
    registry that starts with no keys and accepts everything is the failure
    this ordering forecloses. Fail closed on auth; the fail-OPEN posture in
    `stage1/hooks` is correct there and would be wrong here.
    """
    target = path or os.environ.get(KEYS_ENV_VAR)
    if not target:
        return {}
    p = Path(target)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items() if isinstance(v, str) and v}


def signing_string(method: str, path: str, timestamp: str, body: bytes) -> str:
    digest = hashlib.sha256(body or b"").hexdigest()
    return f"{method.upper()}\n{path}\n{timestamp}\n{digest}"


def sign(secret: str, method: str, path: str, timestamp: str, body: bytes) -> str:
    """The client side, exported so tests and the example publishers share one implementation.

    A second hand-written copy of this in a publisher example is how a signing
    scheme drifts; `stage1/registry/publish.py` imports this function.
    """
    return hmac.new(
        secret.encode(),
        signing_string(method, path, timestamp, body).encode(),
        hashlib.sha256,
    ).hexdigest()


def authenticate(
    headers: Mapping[str, str],
    method: str,
    path: str,
    body: bytes,
    keys: Optional[Mapping[str, str]] = None,
    now: Optional[float] = None,
) -> str:
    """Return the authenticated actor, or raise `Unauthenticated`.

    Header lookup is case-insensitive because the ASGI/`requests`/`curl` paths
    disagree on case and a scheme that works from one client and not another
    reads as "the registry is flaky".
    """
    lowered = {k.lower(): v for k, v in headers.items()}
    actor = (lowered.get(ACTOR_HEADER) or "").strip()
    timestamp = (lowered.get(TIMESTAMP_HEADER) or "").strip()
    presented = (lowered.get(SIGNATURE_HEADER) or "").strip()

    if not actor or not timestamp or not presented:
        raise _unauthenticated(
            "missing actor, timestamp or signature header",
            f"Send {ACTOR_HEADER}, {TIMESTAMP_HEADER} (unix seconds) and "
            f"{SIGNATURE_HEADER} (hex HMAC-SHA256 over "
            f"'METHOD\\npath\\ntimestamp\\nsha256(body)').",
        )

    try:
        sent_at = float(timestamp)
    except ValueError:
        raise _unauthenticated(
            f"timestamp {timestamp!r} is not unix seconds",
            f"Set {TIMESTAMP_HEADER} to the current time in unix seconds.",
        )

    current = time.time() if now is None else now
    if abs(current - sent_at) > MAX_SKEW_S:
        raise _unauthenticated(
            f"timestamp is {abs(current - sent_at):.0f}s from server time "
            f"(window is {MAX_SKEW_S}s)",
            "Re-sign the request with the current time. If this repeats, the "
            "client's clock is wrong rather than the request.",
        )

    table = load_keys() if keys is None else keys
    secret = table.get(actor)
    if not secret:
        # Deliberately the same message as a bad signature: distinguishing
        # "no such actor" from "wrong secret" turns this endpoint into an
        # identity oracle for anyone who can reach it.
        raise _unauthenticated(
            "actor or signature rejected",
            "Check the actor name and shared secret against the registry's key file.",
        )

    expected = sign(secret, method, path, timestamp, body)
    if not hmac.compare_digest(expected, presented):
        raise _unauthenticated(
            "actor or signature rejected",
            "Check the actor name and shared secret against the registry's key file. "
            "The signed string is 'METHOD\\npath\\ntimestamp\\nsha256(body)' with the "
            "path exactly as sent, query string excluded.",
        )
    return actor
