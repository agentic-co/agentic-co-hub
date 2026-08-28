"""The MCP encoding — the same core, over stdio, for a harness that speaks it.

docs/architecture.md: "Two encodings over one semantic core: plain HTTP, and MCP
for harnesses that speak it. Conformance is tested against the core and HTTP; a
third encoding is added when it ships, not before." This module is that second
encoding. It adds no behaviour `app.py` does not already have — every tool here
calls straight into `leases` / `snapshots` / `events` / `work` / `sop`, the same
functions the HTTP handlers call. Reimplementing the lease fence or the scope
intersection at this layer would mean two protocols that can drift; there is
exactly one, and this is a second door into it.

**Tool count is capped at nine, and the cap is enforced by a test, not memory.**
A large surface costs every calling harness context on every single tool-choice
decision it makes — that cost is paid by every conversation, not just this one.
Adding a tenth tool means deleting one, not extending the budget.

**Config deliberately does NOT import `agentco.app`.** `app.py` pulls in FastAPI,
which lives in the `server` extra; this module lives in the `mcp` extra, and a
deployment that only wants the MCP surface should not have to install a web
framework to get it. The registry-db env var name and default are duplicated
here rather than imported, small enough that drift would be caught by the
tests in both modules, and the alternative — MCP requiring `server` — is worse.

**Identity has no signature to check.** Over HTTP the actor comes from an HMAC
header, never the payload (agentco/auth.py) — a client could otherwise file a
claim in someone else's name. Stdio has no request to sign; the actor is
whatever `AGENTCO_ACTOR` says at process start, which is the harness's own
`.mcp.json` entry asserting its own identity once, the same way a shared
secret does. It is exactly as trustworthy as the process that launched it.
"""

from __future__ import annotations

import json
import os
from typing import Optional

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from agentco import db, events as events_module, leases, scope, snapshots
from agentco.errors import Refusal
from agentco.keys import NaturalKeyError
from agentco.sop import SopLibrary
from agentco.work import DEFAULT_LEASE_TTL_S, CapabilityError, Queue, TERMINAL, WorkError, WorkStatus

DB_ENV_VAR = "AGENTCO_REGISTRY_DB"
DEFAULT_DB = "registry.sqlite3"

WORK_STORE_ENV_VAR = "AGENTCO_WORK_STORE"
DEFAULT_WORK_STORE = "work.jsonl"

SOP_STORE_ENV_VAR = "AGENTCO_SOP_STORE"
DEFAULT_SOP_STORE = "sops.jsonl"

ACTOR_ENV_VAR = "AGENTCO_ACTOR"
# A generic default, same posture as app.py's DEFAULT_OPERATOR = "operator" —
# it works out of the box for a single harness and is the first thing a
# multi-harness deployment overrides in its own `.mcp.json` entry.
DEFAULT_ACTOR = "mcp-actor"


def resolve_db_path(path: Optional[str] = None) -> str:
    return path or os.environ.get(DB_ENV_VAR) or DEFAULT_DB


def resolve_work_store(path: Optional[str] = None) -> str:
    return path or os.environ.get(WORK_STORE_ENV_VAR) or DEFAULT_WORK_STORE


def resolve_sop_store(path: Optional[str] = None) -> str:
    return path or os.environ.get(SOP_STORE_ENV_VAR) or DEFAULT_SOP_STORE


def resolve_actor(actor: Optional[str] = None) -> str:
    return actor or os.environ.get(ACTOR_ENV_VAR) or DEFAULT_ACTOR


def _refuse(exc: Exception) -> ToolError:
    """The one seam every tool raises through, so a refusal is never a bare 500.

    `Refusal.__str__` already renders `"{code}: {message} — {remediation}"`
    (agentco/errors.py), so forwarding it verbatim keeps both halves intact
    for whatever harness is on the other end of the stdio pipe. `WorkError`
    and `NaturalKeyError` predate `Refusal` and carry no separate code, but
    by the same convention in their own modules the message IS the
    remediation, so it travels unchanged rather than being reworded into
    something that no longer matches the library's own words.
    """
    return ToolError(str(exc))


def create_server(
    db_path: Optional[str] = None,
    work_store: Optional[str] = None,
    sop_store: Optional[str] = None,
    actor: Optional[str] = None,
) -> FastMCP:
    """Build the server. Paths/actor are injectable so tests need no env vars.

    One `conn`/`queue`/`library` per server instance, closed over by every
    tool below — the same "one connection for the process" posture as
    `app.create_app`, and for the same reason: this is a single-node stage-1
    surface, not a pool of anything.
    """
    conn = db.connect(resolve_db_path(db_path))
    queue = Queue(resolve_work_store(work_store))
    library = SopLibrary(resolve_sop_store(sop_store))
    who = resolve_actor(actor)

    mcp = FastMCP(
        name="agentco",
        instructions=(
            "Coordination primitives for a team running more than one agent "
            "harness: advisory scope leases, pointer-only snapshots, a "
            "resumable change feed, a fenced work queue, and versioned SOPs. "
            "Nothing here blocks your work — every refusal names what to do "
            "instead. Call whoami first to confirm identity and store config."
        ),
    )

    @mcp.tool(name="claim_scope")
    def claim_scope(
        repo: str,
        prefixes: list[str],
        intent: str,
        holder: Optional[str] = None,
        ttl_seconds: int = leases.DEFAULT_TTL_S,
    ) -> dict:
        """Open an advisory ScopeLease: 'I am working in these directories, with this intent.'

        Any conflicting live lease on an intersecting prefix comes back in
        THIS response, not just the event feed — the caller learns about its
        own overlap in the same round trip that created the claim. Advisory
        only: a conflict is information for two people, never a block.
        """
        try:
            return leases.claim(
                conn,
                actor=who,
                repo=repo,
                prefixes=prefixes,
                intent=intent,
                holder=holder,
                ttl_seconds=ttl_seconds,
            )
        except Refusal as exc:
            raise _refuse(exc) from exc

    @mcp.tool(name="release_scope")
    def release_scope(lease_uid: str, action: str = "released") -> dict:
        """Close a lease this actor holds.

        Pass `action="narrowed"` or `action="released_due_to_conflict"` when a
        reported conflict actually changed what you did — that feeds the
        registry's own precision self-audit (conflicts fired ÷ acted on). A
        release you were always going to do anyway is not "acting on" one.
        Releasing the same lease twice is a normal duplicate, not an error.
        """
        try:
            return leases.release(conn, actor=who, lease_uid=lease_uid, action=action)
        except Refusal as exc:
            raise _refuse(exc) from exc

    @mcp.tool(name="snapshot")
    def snapshot(
        artifact_uri: str,
        purpose: str,
        ttl_days: int = snapshots.DEFAULT_TTL_DAYS,
    ) -> dict:
        """Record 'I am working from this version of that' — a pointer, never a copy.

        Only a cheap version token is kept (git SHA, content hash, HTTP eTag);
        the artifact body is never fetched or stored. A scheme with no
        registered resolver is still recorded — it just cannot report
        divergence yet, and the receipt says exactly that rather than failing
        the write.
        """
        try:
            return snapshots.take(conn, actor=who, artifact_uri=artifact_uri, purpose=purpose, ttl_days=ttl_days)
        except Refusal as exc:
            raise _refuse(exc) from exc

    @mcp.tool(name="events")
    def read_events(
        since: Optional[str] = None,
        limit: int = events_module.DEFAULT_LIMIT,
        kind: Optional[str] = None,
    ) -> dict:
        """Read the change feed after an opaque cursor. Omit `since` to start from the beginning.

        `nextCursor` must be passed back verbatim on the following call — it
        is not a timestamp, carries no meaning to construct or edit, and a
        malformed one is refused rather than silently reset to the start (which
        would replay months of events and look like the feed is broken).
        """
        try:
            return events_module.read(conn, since=since, limit=limit, kind=kind)
        except Refusal as exc:
            raise _refuse(exc) from exc

    @mcp.tool(name="work_pull")
    def work_pull(
        agent: Optional[str] = None,
        capabilities: Optional[list[str]] = None,
        ttl_seconds: int = DEFAULT_LEASE_TTL_S,
    ) -> Optional[dict]:
        """Claim the next ready work item this identity can run, with a fenced lease.

        `None` means nothing is claimable right now — an empty queue and a
        lost race to another claimant look identical from here, and both are
        the ordinary shape of a poller finding nothing to do this cycle, not
        an error. An item this worker can never satisfy (its `requires` are
        not in `capabilities`) is skipped rather than raised: `ready()`
        deliberately does not pre-filter by capability, because the
        enforcement belongs inside `claim()` under the same lock as the CAS —
        so a poller is expected to move past it to the next candidate exactly
        the way this loop does, rather than stopping on the first miss.
        """
        who_claims = agent or who
        for item in queue.ready(agent=who_claims):
            try:
                claimed = queue.claim(item.id, who_claims, ttl_seconds=ttl_seconds, capabilities=capabilities)
            except CapabilityError:
                continue
            if claimed is not None:
                return json.loads(claimed.to_json())
        return None

    @mcp.tool(name="work_report")
    def work_report(
        item_id: str,
        attempt: int,
        status: str,
        result: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> dict:
        """Report a terminal outcome (done/failed), fenced on the lease attempt it was issued under.

        A mismatch means this item was handed to someone else while the work
        was in flight — the report is refused because the work was
        SUPERSEDED, not lost, and accepting it would overwrite whoever holds
        the item now. A missing item is refused too, rather than answered
        with a bare success a caller could read as "recorded, nothing to say".
        """
        try:
            parsed_status = WorkStatus(status)
        except ValueError:
            raise ToolError(
                f"status {status!r} is not one of "
                f"{', '.join(sorted(s.value for s in TERMINAL))} — "
                f"report_result records terminal outcomes only."
            )
        try:
            updated = queue.report_result(
                item_id, attempt, parsed_status, result=result, idempotency_key=idempotency_key
            )
        except (WorkError, ValueError) as exc:
            raise _refuse(exc) from exc
        if updated is None:
            raise ToolError(
                f"no work item {item_id!r} on this queue — there is nothing to fence "
                f"this report against. Check the id came from work_pull or work_create "
                f"against this same store."
            )
        return json.loads(updated.to_json())

    @mcp.tool(name="work_create")
    def work_create(
        title: str,
        requires: Optional[list[str]] = None,
        blocked_by: Optional[list[str]] = None,
        assigned_agent: Optional[str] = None,
        natural_key: Optional[str] = None,
        source: Optional[str] = None,
        source_id: Optional[str] = None,
        kind: Optional[str] = None,
        subject: Optional[str] = None,
        period: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> dict:
        """File a new work item. A duplicate natural key returns the EXISTING item, not an error.

        Natural-key precedence is explicit > generated > external (agentco/keys.py):
        pass `natural_key` yourself, or all of `kind`+`subject`+`period` for
        recurring generated work, or `source`+`source_id` to mirror an
        external record. A partial key is refused before anything is written,
        because a repaired key would be a silent duplicate or a silent merge.
        """
        try:
            item = queue.create(
                title,
                requires=requires or (),
                blocked_by=blocked_by or (),
                assigned_agent=assigned_agent,
                natural_key=natural_key,
                source=source,
                source_id=source_id,
                kind=kind,
                subject=subject,
                period=period,
                metadata=metadata,
            )
        except (WorkError, NaturalKeyError) as exc:
            raise _refuse(exc) from exc
        return json.loads(item.to_json())

    @mcp.tool(name="sop_get")
    def sop_get(sop_id: str, version: Optional[int] = None) -> Optional[dict]:
        """Read one SOP version, or the active version when `version` is omitted.

        `None` is the normal answer for an unknown id, or one with no active
        version yet — resolving a pin must never fail loudly, or an SOP that
        has since been superseded would become unreadable to every instance
        still pinned to the version it was created under.
        """
        sop = library.get(sop_id, version=version)
        if sop is None:
            return None
        return json.loads(sop.to_json())

    @mcp.tool(name="whoami")
    def whoami() -> dict:
        """What this actor is identified as, and which stores this server is pointed at.

        There is no RBAC yet — every actor can call every tool (stage 2, gated
        on the adoption gate: docs/roadmap.md) — so this exists for a harness's
        first call to confirm its own `.mcp.json` configuration landed on the
        identity and stores it expects, before it stakes a claim or files work
        under the wrong name.
        """
        return {
            "actor": who,
            "enforcement": "advisory",
            "stores": {
                "registryDb": resolve_db_path(db_path),
                "workStore": str(queue.path),
                "sopStore": str(library.path),
            },
            "scope": {"minSegments": scope.MIN_SEGMENTS, "intents": list(scope.INTENTS)},
            "eventKinds": list(events_module.KINDS),
        }

    return mcp


def main() -> None:
    create_server().run(transport="stdio")


if __name__ == "__main__":
    main()
