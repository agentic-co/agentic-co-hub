"""A stdlib-only client. This file IS the adoption instrument.

The largest named risk in the design: "Nobody publishes. Cold start is the highest-probability
failure of the whole programme." the adoption gate counts publishers, so the artifact
that decides the adoption gate is not the server — it is how few minutes it takes a
colleague to make their first accepted call.

So: no dependencies beyond the standard library, one file, copy-pasteable.
A colleague should be able to drop this next to their script and publish
without installing AgentCo, reading the design, or asking anyone for a
package. The signing function is therefore a DECLARED COPY of `auth.sign`,
carrying a hash marker that goes stale — and fails a test — the moment the
original changes. Importing it would be tidier and would destroy the one
property this file exists to have.

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


# vendored-from: agentco/auth.py sha256=cd9e310668a447186d8df3c47e84cc76076911a10cb3e17c12decefd7ba76623
#
# A DECLARED COPY, not an accident, and not an import. This file exists to be
# copy-pasted by someone who never installs the package — that is the whole
# point of it, and importing `agentco.auth` would destroy the property it is
# for. So the duplication stays, and what changes is that it is now DECLARED:
# the hash above pins the exact bytes of `auth.signing_string` + `auth.sign`
# that were copied.
#
# The marker is what makes drift detectable. Edit `auth.sign` without
# re-vendoring and the hash goes stale and the test fails — which is the real
# risk here, because a drifted copy does not fail at import. It produces
# valid-looking signatures the server rejects, so a colleague's first contact
# with the registry is a 401 they cannot debug.
#
# Two docstrings used to claim this file imported the function. Neither was
# true, and each pointed at the other as the safeguard.
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

    # --- the work queue --------------------------------------------------
    #
    # A harness on another machine can now pull work and report on it. The
    # claiming identity is the actor this client authenticates as — the server
    # ignores any agent named in the body, so there is no field here to set it.

    def work_create(self, title: str, **fields: Any) -> dict:
        """File an item. A duplicate natural key returns the EXISTING item.

        Field names are the wire's, not Python's: `blockedBy`, `assignedAgent`,
        `naturalKey`, `sourceId`. Passing `blocked_by` would be silently
        ignored by the server, so it is rejected here where the traceback still
        points at the caller.
        """
        snake = [k for k in fields if "_" in k and k != "definition_of_done"]
        if snake:
            raise ValueError(
                f"use the wire spelling for {', '.join(sorted(snake))} — "
                "blockedBy, assignedAgent, naturalKey, sourceId. The server "
                "ignores unknown fields, so a snake_case key would be dropped "
                "silently and the item filed without it."
            )
        return self._call("POST", "/work", {"title": title, **fields})

    def work_pull(self, capabilities: Optional[list[str]] = None, ttl_seconds: Optional[int] = None) -> dict:
        """Claim the next item this actor can run.

        Returns `{"state": "empty", "item": None}` when there is nothing to do,
        which is an ordinary quiet cycle rather than an error. On success the
        top-level `attempt` is the fence — send it back with the report.
        """
        body: dict[str, Any] = {}
        if capabilities is not None:
            body["capabilities"] = capabilities
        if ttl_seconds:
            body["ttlSeconds"] = ttl_seconds
        return self._call("POST", "/work/pull", body)

    def work_report(
        self,
        item_id: str,
        attempt: int,
        status: str,
        result: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> dict:
        """Report `done` or `failed`, fenced on the attempt the lease was issued under.

        `idempotency_key` makes an honest retry safe: any transport can lose an
        acknowledgement after the server applied the result, and the worker has
        to be able to send the same thing again.
        """
        body: dict[str, Any] = {"attempt": attempt, "status": status}
        if result is not None:
            body["result"] = result
        if idempotency_key is not None:
            body["idempotencyKey"] = idempotency_key
        return self._call("POST", f"/work/{item_id}/report", body)

    def work_list(self, status: Optional[str] = None, ready: bool = False) -> dict:
        """The queue as it stands. `ready=True` asks what is claimable right now."""
        if ready:
            return self._call("GET", "/work", None, "?ready=1")
        return self._call("GET", "/work", None, f"?status={status}" if status else "")

    # --- versioned SOPs --------------------------------------------------

    def sop_create(self, title: str, **body: Any) -> dict:
        """Author version 1 as a DRAFT. Activation is separate and deliberate."""
        return self._call("POST", "/sops", {"title": title, **body})

    def sop_revise(self, sop_id: str, **body: Any) -> dict:
        """Write the next version — this is how a lesson learned becomes shared.

        Unset fields carry forward, so adding one line to `common_mistakes`
        does not blank the other four. The superseded version stays readable.
        """
        return self._call("POST", f"/sops/{sop_id}/revise", body)

    def sop_activate(self, sop_id: str, version: int) -> dict:
        """Make one version the one every reader gets by default."""
        return self._call("POST", f"/sops/{sop_id}/activate", {"version": version})

    def sop_get(self, sop_id: str, version: Optional[int] = None) -> dict:
        """One version, or the active one. A miss is `sop: null`, not an error."""
        return self._call("GET", f"/sops/{sop_id}", None, f"?version={version}" if version else "")

    def sop_list(self) -> dict:
        """Every SOP with an active version."""
        return self._call("GET", "/sops")

    def sop_chain(self, sop_id: str) -> dict:
        """The whole process starting here, with any broken link named."""
        return self._call("GET", f"/sops/{sop_id}/chain")

    def sop_instantiate(self, sop_id: str, **fields: Any) -> dict:
        """File work pinned to this SOP's active version.

        Use this rather than `work_create` whenever the work has a procedure:
        the item then carries which procedure it was run under, which is the
        only thing that makes "did the revision help" answerable later.
        """
        return self._call("POST", f"/sops/{sop_id}/instantiate", fields)
