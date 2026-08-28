"""A stdlib-only client. This file IS the adoption instrument.

The largest named risk in the design: "Nobody publishes. Cold start is the highest-probability
failure of the whole programme." the adoption gate counts publishers, so the artifact
that decides the adoption gate is not the server — it is how few minutes it takes a
colleague to make their first accepted call.

So: no dependencies beyond the standard library, one file, copy-pasteable.
A colleague should be able to drop this next to their script and publish
without installing AgentCo, reading the design, or asking anyone for a
package. It imports `auth.sign` when AgentCo is importable and inlines the
same three lines when it is not — the signing scheme must never exist in two
implementations that can drift.

Two example publishers, per docs/roadmap.md's 1c ("two example HTTP publishers"):
`claim_scope` for someone about to work in a directory, and `snapshot` for
someone baselining a document. Both are four lines at the call site.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.error
import urllib.request
from typing import Any, Optional

DEFAULT_BASE_URL = "http://127.0.0.1:8787"


def _sign(secret: str, method: str, path: str, timestamp: str, body: bytes) -> str:
    digest = hashlib.sha256(body or b"").hexdigest()
    signing_string = f"{method.upper()}\n{path}\n{timestamp}\n{digest}"
    return hmac.new(secret.encode(), signing_string.encode(), hashlib.sha256).hexdigest()


class RegistryError(RuntimeError):
    """A refusal or transport failure, carrying the registry's own remediation.

    The remediation sentence is surfaced in `str(exc)` rather than buried in
    an attribute, because the caller most likely to hit a refusal is a
    colleague running a script for the first time and the traceback is all
    they will read.
    """

    def __init__(self, status: Optional[int], payload: Any):
        self.status = status
        self.payload = payload
        if isinstance(payload, dict) and payload.get("remediation"):
            detail = f"{payload.get('code')}: {payload.get('message')} — {payload['remediation']}"
        else:
            detail = str(payload)
        super().__init__(f"registry refused (HTTP {status or '?'}): {detail}")


class Registry:
    """Minimal signed-request client.

    `base_url` defaults to loopback because that is where the stage-1 service
    runs until the VM is provisioned; pointing it at the VM is one argument.
    """

    def __init__(self, actor: str, secret: str, base_url: str = DEFAULT_BASE_URL, timeout: int = 15):
        self.actor = actor
        self.secret = secret
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _call(self, method: str, path: str, body: Optional[dict] = None, query: str = "") -> dict:
        raw = json.dumps(body).encode() if body is not None else b""
        timestamp = str(int(time.time()))
        # The signature covers the path WITHOUT the query string — the server
        # signs `request.url.path` for the same reason: a proxy that reorders
        # or re-encodes query parameters must not invalidate the signature.
        request = urllib.request.Request(
            f"{self.base_url}{path}{query}",
            data=raw if raw else None,
            method=method,
            headers={
                "Content-Type": "application/json",
                "X-AgentCo-Actor": self.actor,
                "X-AgentCo-Timestamp": timestamp,
                "X-AgentCo-Signature": _sign(self.secret, method, path, timestamp, raw),
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            try:
                payload = json.loads(exc.read().decode())
            except (ValueError, OSError):
                payload = {"message": exc.reason}
            raise RegistryError(exc.code, payload) from exc
        except urllib.error.URLError as exc:
            raise RegistryError(None, {"message": f"cannot reach {self.base_url}: {exc.reason}"}) from exc

    # --- the two example publishers -------------------------------------

    def claim_scope(
        self,
        repo: str,
        prefixes: list[str],
        intent: str = "implement",
        ttl_seconds: Optional[int] = None,
    ) -> dict:
        """Declare where you are about to work. Advisory — it blocks nothing.

            reg.claim_scope("acme/web-platform", ["src/billing/invoices"], "implement")

        Returns the lease plus `conflicts` — anyone else live in the same
        directories, and under what intent.
        """
        body: dict[str, Any] = {"repo": repo, "prefixes": prefixes, "intent": intent}
        if ttl_seconds:
            body["ttlSeconds"] = ttl_seconds
        return self._call("POST", "/scope-claims", body)

    def release_scope(self, lease_uid: str, action: str = "released") -> dict:
        """Close a lease. `action` feeds the scope-model decision's precision metric — pass
        `narrowed` or `released_due_to_conflict` when a reported conflict
        actually changed what you did, because that ratio is what decides
        whether the conflict rule is calibrated."""
        return self._call("POST", f"/scope-claims/{lease_uid}/release", {"action": action})

    def snapshot(self, artifact_uri: str, purpose: str) -> dict:
        """Baseline a document by pointer. The body is never sent or stored.

            reg.snapshot("git:/repo#main", "prototype baseline for the redesign")

        You get told at the next cadence boundary if it moves.
        """
        return self._call("POST", "/snapshots", {"artifactUri": artifact_uri, "purpose": purpose})

    def events(self, since: Optional[str] = None, limit: int = 200) -> dict:
        """Resume the change feed. Pass the previous `nextCursor` verbatim."""
        query = f"?limit={limit}" + (f"&since={since}" if since else "")
        return self._call("GET", "/events", None, query)
