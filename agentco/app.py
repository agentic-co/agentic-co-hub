"""Plain HTTP over one SQLite file and two JSONL stores. That is stage 1b.

    `POST /scope-claims`   — open a ScopeLease (the scope-model decision (docs/decisions/0001) scope model)
    `POST /snapshots`      — record a pointer + version token, never a body
    `GET  /events?since=`  — the change feed, opaque resumable cursor

Plus two reads that are not new surface, because they are the same data
answering the questions stage 1d has to answer anyway:

    `POST /scope-claims/{uid}/release`  — the other half of claim_scope
                                          (the verb is open/renew/release)
    `GET  /metrics`                     — the adoption gate, latency, conflict precision

And the work queue and SOP library, which are here for one reason: every
surface that *delivers* something to a harness — the MCP server, `inject`, the
session hook — reads the stores off the local filesystem. So a queue could be
pushed to from any machine and pulled from only by a process sitting on the
registry's own disk, which is not a coordination layer for an organisation, it
is one for a laptop.

    `POST /work`                  — file an item (duplicate natural key = loud no-op)
    `POST /work/pull`             — claim the next ready item, fenced
    `POST /work/{id}/report`      — terminal outcome, fenced on the attempt
    `GET  /work?status=|ready=`   — the queue as it stands
    `POST /sops`                  — author version 1, as a draft
    `POST /sops/{id}/revise`      — the next version; this is how a lesson travels
    `POST /sops/{id}/activate`    — make one version the default for every reader
    `GET  /sops`, `GET /sops/{id}?version=`

The queue and the library are FILES under an OS lock, not tables. One process
holds that lock, which is why the MCP server must not be pointed at the same
paths while this app is serving them — two writers is the blindness the
registry exists to remove, in a different store.

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
from agentco.keys import NaturalKeyError
from agentco.sop import LINK_FIELD, TEXT_FIELDS, SopError, SopLibrary, resolve_sop_store
from agentco.stores import open_queue, open_sop_library, resolve_registry_db
from agentco.work import (
    DEFAULT_LEASE_TTL_S,
    CapabilityError,
    Queue,
    TERMINAL,
    WorkError,
    WorkStatus,
    resolve_work_store,
)

# Derived, never hand-kept: a literal list here would silently drop any field
# added to sop.py, and the symptom is an SOP that saves without the half the
# author just wrote.
SOP_BODY_KEYS = (*TEXT_FIELDS, "common_mistakes", LINK_FIELD)

DB_ENV_VAR = "AGENTCO_REGISTRY_DB"
DEFAULT_DB = "registry.sqlite3"


def _int(payload: dict, key: str, default: int) -> int:
    """Coerce one integer field, refusing rather than exploding.

    `int(payload.get(...))` on caller-supplied text raises ValueError, which
    the generic handler renders as HTTP 500 "this is a registry bug" — telling
    a caller who sent a typo that the server is broken. Every integer that
    crosses the wire goes through here instead.
    """
    raw = payload.get(key)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise Refusal(
            code="not_an_integer",
            message=f"{key} must be a whole number, got {raw!r}",
            remediation=f"Send {key} as a JSON number, e.g. {{\"{key}\": {default}}}.",
            http_status=400,
        ) from None


def _work_refusal(exc: Exception) -> Refusal:
    """Translate the queue's and library's own exceptions into refusals.

    These modules predate `Refusal` and raise their own types, in which the
    message IS the remediation by their own convention — so it travels
    unchanged rather than being reworded into something that no longer matches
    the library's words. What this adds is the part only the HTTP surface can
    decide: the status code, and a stable code to branch on.

    Nothing here may fall through to the generic 500. A fenced report arriving
    late is the queue working exactly as designed; reporting it as a server bug
    would teach a worker its correct behaviour is a crash.
    """
    if isinstance(exc, NaturalKeyError):
        return Refusal(code="natural_key_invalid", message=str(exc),
                       remediation=str(exc), http_status=400)
    if isinstance(exc, CapabilityError):
        return Refusal(code="capability_mismatch", message=str(exc),
                       remediation=str(exc), http_status=409)
    if isinstance(exc, WorkError):
        # LeaseError and BlockedError both land here. 409 rather than 422: the
        # request was well-formed and lost to the state of the world, which is
        # a conflict, not a malformed body.
        return Refusal(code="work_conflict", message=str(exc),
                       remediation=str(exc), http_status=409)
    if isinstance(exc, SopError):
        return Refusal(code="sop_refused", message=str(exc),
                       remediation=str(exc), http_status=422)
    return Refusal(code="invalid_request", message=str(exc),
                   remediation=str(exc), http_status=400)

OPERATOR_ENV_VAR = "AGENTCO_REGISTRY_OPERATOR"
# The identity excluded from the adoption gate's own publisher count —
# whoever is building this. Configured, never inferred: guessing "whoever
# has the most calls" would silently exclude the first real power user.
DEFAULT_OPERATOR = "operator"


def resolve_db_path(path: Optional[str] = None) -> str:
    return resolve_registry_db(path, DB_ENV_VAR, DEFAULT_DB)


def create_app(
    db_path: Optional[str] = None,
    keys: Optional[dict[str, str]] = None,
    operator: Optional[str] = None,
    work_store: Optional[str] = None,
    sop_store: Optional[str] = None,
) -> FastAPI:
    """Build the ASGI app. `keys`/`operator` are injectable so tests need no env.

    One connection for the process, shared across worker threads: SQLite in
    WAL mode with short transactions handles this, and a connection pool for a
    single-node stage-1 service is machinery in search of a problem.
    """
    conn = db.connect(resolve_db_path(db_path))
    # On the default backend the queue and the library are FILES, and one
    # process holds the lock — the same single-writer posture the container
    # documents for the registry DB, and the reason the MCP server must not be
    # pointed at these same paths while this app is serving them. With
    # `AGENTCO_DB` set they are tables in the registry's own database and that
    # restriction lifts, which is most of why the backend exists.
    queue = open_queue(work_store)
    library = open_sop_library(sop_store)
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

    # -- the work queue, over the wire ------------------------------------
    #
    # Same primitives the MCP surface exposes, reachable by a harness that is
    # not on this filesystem. That was the gap: everything that *delivers* work
    # to an agent read the JSONL directly, so a queue could be pushed to from
    # anywhere and pulled from only from localhost.
    #
    # The claiming identity is the AUTHENTICATED actor, never a payload field.
    # A worker that can name itself in the body can take another worker's lease
    # by typing its name, and the fence would faithfully record the theft.

    @app.post("/work")
    async def post_work_create(request: Request) -> JSONResponse:
        """File an item. A duplicate natural key returns the EXISTING item.

        Deliberately identical to `work_create` on the MCP surface, including
        the loud no-op: two surfaces onto one queue that disagree about what a
        duplicate means is worse than either rule on its own.
        """

        def work(actor: str, payload: dict) -> dict:
            try:
                item = queue.create(
                    payload.get("title", ""),
                    requires=payload.get("requires") or (),
                    blocked_by=payload.get("blockedBy") or (),
                    assigned_agent=payload.get("assignedAgent"),
                    natural_key=payload.get("naturalKey"),
                    source=payload.get("source"),
                    source_id=payload.get("sourceId"),
                    kind=payload.get("kind"),
                    subject=payload.get("subject"),
                    period=payload.get("period"),
                    metadata=payload.get("metadata"),
                )
            except (WorkError, NaturalKeyError, ValueError) as exc:
                raise _work_refusal(exc) from exc
            return {"state": "accepted", "item": json.loads(item.to_json())}

        return await _handle(request, "work_create", work)

    @app.post("/work/pull")
    async def post_work_pull(request: Request) -> JSONResponse:
        """Claim the next ready item this actor can run, with a fenced lease.

        An empty queue and a lost race are the same answer — `state: "empty"` —
        because they are the same situation for a poller: nothing to do this
        cycle. Neither is an error, and answering 404 would make an ordinary
        quiet minute look like a broken deployment.
        """

        def work(actor: str, payload: dict) -> dict:
            capabilities = payload.get("capabilities")
            ttl = _int(payload, "ttlSeconds", DEFAULT_LEASE_TTL_S)
            for candidate in queue.ready(agent=actor):
                try:
                    claimed = queue.claim(candidate.id, actor, ttl_seconds=ttl, capabilities=capabilities)
                except CapabilityError:
                    # Not fatal, and not this worker's problem: skip to the next
                    # candidate exactly as the MCP poller does. `ready()` does
                    # not pre-filter by capability on purpose.
                    continue
                except WorkError as exc:
                    raise _work_refusal(exc) from exc
                if claimed is not None:
                    item = json.loads(claimed.to_json())
                    return {
                        "state": "leased",
                        "item": item,
                        # Surfaced at the top level because it is the one field
                        # the caller MUST send back, and burying the fence in a
                        # nested object is how a report gets sent without it.
                        "attempt": item.get("lease_attempt"),
                    }
            return {"state": "empty", "item": None}

        return await _handle(request, "work_pull", work)

    @app.post("/work/{item_id}/report")
    async def post_work_report(item_id: str, request: Request) -> JSONResponse:
        """Report a terminal outcome, fenced on the attempt the lease was issued under."""

        def work(actor: str, payload: dict) -> dict:
            raw_status = payload.get("status", "")
            try:
                parsed = WorkStatus(raw_status)
            except ValueError:
                raise Refusal(
                    code="not_terminal",
                    message=f"status {raw_status!r} is not a terminal outcome",
                    remediation=(
                        "Report one of "
                        f"{', '.join(sorted(s.value for s in TERMINAL))}. "
                        "A lease is released by reporting, not by reporting progress."
                    ),
                    http_status=400,
                ) from None
            if parsed not in TERMINAL:
                raise Refusal(
                    code="not_terminal",
                    message=f"{parsed.value} is not a terminal outcome",
                    remediation=(
                        "Report one of "
                        f"{', '.join(sorted(s.value for s in TERMINAL))}."
                    ),
                    http_status=400,
                )
            attempt = _int(payload, "attempt", -1)
            if attempt < 0:
                raise Refusal(
                    code="attempt_required",
                    message="a report must carry the lease attempt it was issued under",
                    remediation=(
                        "Send the `attempt` returned by POST /work/pull. Without the "
                        "fence, a report that arrived late would overwrite whoever "
                        "holds the item now."
                    ),
                    http_status=400,
                )
            try:
                updated = queue.report_result(
                    item_id,
                    attempt,
                    parsed,
                    result=payload.get("result"),
                    idempotency_key=payload.get("idempotencyKey"),
                )
            except (WorkError, ValueError) as exc:
                raise _work_refusal(exc) from exc
            if updated is None:
                raise Refusal(
                    code="work_item_unknown",
                    message=f"no work item {item_id!r} on this queue",
                    remediation=(
                        "There is nothing to fence this report against. Check the id "
                        "came from POST /work/pull or POST /work against this same "
                        "registry."
                    ),
                    http_status=404,
                )
            return {"state": "accepted", "item": json.loads(updated.to_json())}

        return await _handle(request, "work_report", work)

    @app.get("/work")
    async def get_work(request: Request) -> JSONResponse:
        """List the queue. `?status=` filters; `?ready=1` asks what is claimable now."""

        def work(actor: str, payload: dict) -> dict:
            params = request.query_params
            if params.get("ready"):
                items = queue.ready(agent=params.get("agent"))
            else:
                raw = params.get("status")
                if raw:
                    try:
                        wanted = WorkStatus(raw)
                    except ValueError:
                        raise Refusal(
                            code="unknown_status",
                            message=f"{raw!r} is not a work status",
                            remediation=(
                                "Use one of "
                                f"{', '.join(sorted(s.value for s in WorkStatus))}."
                            ),
                            http_status=400,
                        ) from None
                else:
                    wanted = None
                items = queue.list(status=wanted)
            return {"state": "accepted", "items": [json.loads(i.to_json()) for i in items]}

        return await _handle(request, "work_list", work)

    # -- the SOP library, over the wire ------------------------------------
    #
    # Versioned procedure, shared between machines. A revision is how a lesson
    # learned on one harness reaches every other one: the old version is never
    # edited, so instances pinned to it stay readable and the evidence for
    # whether the revision helped survives.

    @app.post("/sops")
    async def post_sop_create(request: Request) -> JSONResponse:
        """Author version 1, as a DRAFT — activation is a separate, deliberate act."""

        def work(actor: str, payload: dict) -> dict:
            body = {k: payload[k] for k in SOP_BODY_KEYS if k in payload}
            try:
                sop = library.create(payload.get("title", ""), **body)
            except (SopError, ValueError) as exc:
                raise _work_refusal(exc) from exc
            return {"state": "accepted", "sop": json.loads(sop.to_json())}

        return await _handle(request, "sop_create", work)

    @app.post("/sops/{sop_id}/revise")
    async def post_sop_revise(sop_id: str, request: Request) -> JSONResponse:
        """Write the next version. Unset fields carry forward; the old one is superseded."""

        def work(actor: str, payload: dict) -> dict:
            body = {k: payload[k] for k in SOP_BODY_KEYS if k in payload}
            try:
                sop = library.revise(sop_id, title=payload.get("title"), **body)
            except (SopError, ValueError) as exc:
                raise _work_refusal(exc) from exc
            return {"state": "accepted", "sop": json.loads(sop.to_json())}

        return await _handle(request, "sop_revise", work)

    @app.post("/sops/{sop_id}/activate")
    async def post_sop_activate(sop_id: str, request: Request) -> JSONResponse:
        """Make one version the one every reader gets by default."""

        def work(actor: str, payload: dict) -> dict:
            version = _int(payload, "version", -1)
            if version < 1:
                raise Refusal(
                    code="version_required",
                    message="activate names a specific version",
                    remediation="Send {\"version\": N} — the version you mean to make active.",
                    http_status=400,
                )
            try:
                sop = library.activate(sop_id, version)
            except (SopError, ValueError) as exc:
                raise _work_refusal(exc) from exc
            return {"state": "accepted", "sop": json.loads(sop.to_json())}

        return await _handle(request, "sop_activate", work)

    @app.post("/sops/{sop_id}/instantiate")
    async def post_sop_instantiate(sop_id: str, request: Request) -> JSONResponse:
        """File work that PINS this SOP version.

        Here rather than assembled by the caller because the check that matters
        is `instantiate`'s own: a draft is refused. Building the pin client-side
        from a `GET /sops/{id}` would work right up until someone passed an
        explicit version, and then it would hand somebody a half-written
        procedure carrying the authority of a published one.

        The pin is immutable for the life of the item. Later revisions do not
        reach back — that is what makes outcomes comparable across versions.
        """

        def work(actor: str, payload: dict) -> dict:
            version = payload.get("version")
            try:
                item = library.instantiate(
                    sop_id,
                    queue,
                    title=payload.get("title"),
                    version=int(version) if version is not None else None,
                    requires=payload.get("requires") or (),
                    blocked_by=payload.get("blockedBy") or (),
                    assigned_agent=payload.get("assignedAgent"),
                    natural_key=payload.get("naturalKey"),
                    source=payload.get("source"),
                    source_id=payload.get("sourceId"),
                    kind=payload.get("kind"),
                    subject=payload.get("subject"),
                    period=payload.get("period"),
                    metadata=payload.get("metadata"),
                )
            except (SopError, WorkError, NaturalKeyError, ValueError) as exc:
                raise _work_refusal(exc) from exc
            return {"state": "accepted", "item": json.loads(item.to_json())}

        return await _handle(request, "sop_instantiate", work)

    @app.get("/sops/{sop_id}/chain")
    async def get_sop_chain(sop_id: str, request: Request) -> JSONResponse:
        """The process this SOP starts, one step per entry.

        Served rather than walked client-side because the interesting answers
        are the negative ones — a link naming an SOP that does not exist, or one
        with no active version — and a client walking `next_sop` with
        `GET /sops/{id}` would see both as an ordinary end of chain.
        """

        def work(actor: str, payload: dict) -> dict:
            steps = library.chain(sop_id)
            return {
                "state": "accepted",
                "steps": steps,
                "broken": [s for s in steps if s["state"] != "active"],
            }

        return await _handle(request, "sop_chain", work)

    @app.get("/sops")
    async def get_sops(request: Request) -> JSONResponse:
        """Every SOP with an active version."""

        def work(actor: str, payload: dict) -> dict:
            return {"state": "accepted", "sops": [json.loads(s.to_json()) for s in library.list_active()]}

        return await _handle(request, "sop_list", work)

    @app.get("/sops/{sop_id}")
    async def get_sop(sop_id: str, request: Request) -> JSONResponse:
        """One version, or the active one when `?version=` is omitted.

        A miss is `sop: null` with HTTP 200, not a 404. Resolving a pin must
        never fail loudly — an instance pinned to a version that was later
        superseded still has to be able to read it, and a caller that treats
        "no active version yet" as an outage stops asking.
        """

        def work(actor: str, payload: dict) -> dict:
            raw = request.query_params.get("version")
            version = None
            if raw:
                try:
                    version = int(raw)
                except (TypeError, ValueError):
                    raise Refusal(
                        code="not_an_integer",
                        message=f"version must be a whole number, got {raw!r}",
                        remediation="Pass ?version=2, or omit it for the active version.",
                        http_status=400,
                    ) from None
            sop = library.get(sop_id, version=version)
            return {"state": "accepted", "sop": json.loads(sop.to_json()) if sop else None}

        return await _handle(request, "sop_get", work)

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
