"""Three plain HTTP endpoints over one SQLite file. That is stage 1b.

    `POST /scope-claims`   — open a ScopeLease (the scope-model decision (docs/decisions/0001) scope model)
    `POST /snapshots`      — record a pointer + version token, never a body
    `GET  /events?since=`  — the change feed, opaque resumable cursor

Plus two reads that are not new surface, because they are the same data
answering the questions stage 1d has to answer anyway:

    `POST /scope-claims/{uid}/release`  — the other half of claim_scope
                                          (the verb is open/renew/release)
    `GET  /metrics`                     — the adoption gate, latency, conflict precision

**No admission chain, no kinds, no RBAC, no resource model.** Those are stage
2 and stage 2 only happens if the adoption gate passes (docs/roadmap.md). The handlers here are
deliberately thin: validate, write, record, respond.

Every request is timed and recorded — refusals included — because 1d's
numbers come from `calls` and a refused call is the most diagnostic row in
the table. `_handle` is the single seam that guarantees it: a handler cannot
return without its telemetry row, because the handler does not own the
response path.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from typing import Any, Awaitable, Callable, Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from agentco import auth, db, divergence, events, leases, metrics, snapshots
from agentco.errors import Refusal

DB_ENV_VAR = "AGENTCO_REGISTRY_DB"
DEFAULT_DB = "registry.sqlite3"

OPERATOR_ENV_VAR = "AGENTCO_REGISTRY_OPERATOR"
# The identity excluded from the adoption gate's own publisher count —
# whoever is building this. Configured, never inferred: guessing "whoever
# has the most calls" would silently exclude the first real power user.
DEFAULT_OPERATOR = "operator"


def resolve_db_path(path: Optional[str] = None) -> str:
    return path or os.environ.get(DB_ENV_VAR) or DEFAULT_DB


def create_app(
    db_path: Optional[str] = None,
    keys: Optional[dict[str, str]] = None,
    operator: Optional[str] = None,
) -> FastAPI:
    """Build the ASGI app. `keys`/`operator` are injectable so tests need no env.

    One connection for the process, shared across worker threads: SQLite in
    WAL mode with short transactions handles this, and a connection pool for a
    single-node stage-1 service is machinery in search of a problem.
    """
    conn = db.connect(resolve_db_path(db_path))
    app = FastAPI(
        title="AgentCo scope + snapshot registry (stage 1b)",
        description=(
            "Scope claims, snapshot pointers, and a resumable change feed. "
            "Advisory only — nothing here blocks anyone's work."
        ),
    )
    app.state.conn = conn
    app.state.keys = keys
    app.state.operator = operator or os.environ.get(OPERATOR_ENV_VAR) or DEFAULT_OPERATOR

    async def _handle(
        request: Request,
        verb: str,
        work: Callable[[str, dict], Awaitable[Any] | Any],
    ) -> JSONResponse:
        """Authenticate → run → record. The only path a response takes.

        Latency is measured around the handler body only, not around request
        parsing by the server, so the number means "server-side time for this
        verb" the way the published SLO is phrased.
        """
        started = time.perf_counter()
        body = await request.body()
        actor = "-"
        try:
            actor = auth.authenticate(
                dict(request.headers),
                request.method,
                request.url.path,
                body,
                keys=app.state.keys,
            )
            payload: dict = {}
            if body:
                try:
                    parsed = json.loads(body)
                except json.JSONDecodeError:
                    raise Refusal(
                        code="bad_json",
                        message="request body is not valid JSON",
                        remediation="Send a JSON object with Content-Type: application/json.",
                        http_status=400,
                    )
                if not isinstance(parsed, dict):
                    raise Refusal(
                        code="bad_json",
                        message="request body must be a JSON object",
                        remediation="Send a JSON object, not an array or scalar.",
                        http_status=400,
                    )
                payload = parsed

            result = work(actor, payload)
            elapsed = (time.perf_counter() - started) * 1000
            metrics.record_call(
                conn,
                verb=verb,
                actor=actor,
                status=str(result.get("state", "accepted")) if isinstance(result, dict) else "accepted",
                latency_ms=elapsed,
            )
            return JSONResponse(result)

        except Refusal as refusal:
            elapsed = (time.perf_counter() - started) * 1000
            metrics.record_call(
                conn,
                verb=verb,
                actor=actor,
                status="refused",
                code=refusal.code,
                latency_ms=elapsed,
            )
            return JSONResponse(refusal.to_dict(), status_code=refusal.http_status)

        except Exception as exc:  # noqa: BLE001 - fail loudly, but as JSON
            elapsed = (time.perf_counter() - started) * 1000
            metrics.record_call(
                conn,
                verb=verb,
                actor=actor,
                status="error",
                code=type(exc).__name__,
                latency_ms=elapsed,
            )
            # Named, never swallowed. A 500 that says nothing teaches the
            # caller the tool lies.
            return JSONResponse(
                {
                    "state": "error",
                    "code": "internal_error",
                    "message": f"{type(exc).__name__}: {exc}",
                    "remediation": "This is a registry bug — the message above is the whole of it.",
                },
                status_code=500,
            )

    @app.post("/scope-claims")
    async def post_scope_claim(request: Request) -> JSONResponse:
        def work(actor: str, payload: dict) -> dict:
            return leases.claim(
                conn,
                actor=actor,
                repo=payload.get("repo", ""),
                prefixes=payload.get("prefixes") or [],
                intent=payload.get("intent", ""),
                holder=payload.get("holder"),
                ttl_seconds=int(payload.get("ttlSeconds") or leases.DEFAULT_TTL_S),
            )

        return await _handle(request, "claim_scope", work)

    @app.post("/scope-claims/{lease_uid}/release")
    async def post_release(lease_uid: str, request: Request) -> JSONResponse:
        def work(actor: str, payload: dict) -> dict:
            return leases.release(
                conn,
                actor=actor,
                lease_uid=lease_uid,
                action=payload.get("action") or "released",
            )

        return await _handle(request, "release_scope", work)

    @app.post("/snapshots")
    async def post_snapshot(request: Request) -> JSONResponse:
        def work(actor: str, payload: dict) -> dict:
            return snapshots.take(
                conn,
                actor=actor,
                artifact_uri=payload.get("artifactUri", ""),
                purpose=payload.get("purpose", ""),
            )

        return await _handle(request, "snapshot", work)

    @app.get("/events")
    async def get_events(request: Request) -> JSONResponse:
        def work(actor: str, payload: dict) -> dict:
            params = request.query_params
            return events.read(
                conn,
                since=params.get("since"),
                limit=int(params.get("limit") or events.DEFAULT_LIMIT),
                kind=params.get("kind"),
            )

        return await _handle(request, "events", work)

    @app.get("/metrics")
    async def get_metrics(request: Request) -> JSONResponse:
        def work(actor: str, payload: dict) -> dict:
            return {
                "gate1": metrics.gate1_status(conn, operator=app.state.operator),
                "latency": metrics.verb_latency(conn),
                "timeToFirstEvent": metrics.time_to_first_event(conn),
                "conflictPrecision": metrics.conflict_precision(conn),
            }

        return await _handle(request, "metrics", work)

    @app.get("/divergence")
    async def get_divergence(request: Request) -> JSONResponse:
        """Read the accumulated digest WITHOUT delivering it.

        Delivery marks pointers as reported and is a cadence-boundary action
        (`agentco-registry digest --deliver`). A GET that silently consumed
        the backlog would mean whoever refreshed the page first was the only
        person who ever saw it.
        """

        def work(actor: str, payload: dict) -> dict:
            return divergence.collect(conn)

        return await _handle(request, "divergence", work)

    return app


def get_conn(app: FastAPI) -> sqlite3.Connection:
    return app.state.conn
