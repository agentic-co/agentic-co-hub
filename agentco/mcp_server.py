"""The MCP encoding — the same core, over stdio, for a harness that speaks it.

docs/architecture.md: "Two encodings over one semantic core: plain HTTP, and MCP
for harnesses that speak it. Conformance is tested against the core and HTTP; a
third encoding is added when it ships, not before." This module is that second
encoding. It adds no behaviour `app.py` does not already have — every tool here
calls straight into `leases` / `snapshots` / `events` / `work` / `sop`, the same
functions the HTTP handlers call. Reimplementing the lease fence or the scope
intersection at this layer would mean two protocols that can drift; there is
exactly one, and this is a second door into it.

**Tool count is capped at twelve, and the cap is enforced by a test, not memory.**
A large surface costs every calling harness context on every single tool-choice
decision it makes — that cost is paid by every conversation, not just this one.
0002-participation-ladder.md moved the ceiling from nine to twelve to reserve
room for `attest`, `sop_revise` and `sop_activate`. `attest` shipped with the
Phase 1 transports; the other two landed with Phase 4, so all twelve are
registered below and no names are held. Adding a thirteenth tool means
deleting one, not extending the budget. A published byte budget
(`tests/test_mcp_server.py`) sits alongside the count, because the count is a
proxy for context cost, and a proxy that stops tracking the thing it measures
is worse than no proxy.

**Config deliberately does NOT import `agentco.app`.** `app.py` pulls in FastAPI,
which lives in the `server` extra; this module lives in the `mcp` extra, and a
deployment that only wants the MCP surface should not have to install a web
framework to get it. The registry-db env var name and default are duplicated
here rather than imported, small enough that drift would be caught by the
tests in both modules, and the alternative — MCP requiring `server` — is worse.

**Identity has no signature to check — in local mode.** Over HTTP the actor
comes from an HMAC header, never the payload (agentco/auth.py) — a client could
otherwise file a claim in someone else's name. Stdio has no request to sign; the
actor is whatever `AGENTCO_ACTOR` says at process start, which is the harness's
own `.mcp.json` entry asserting its own identity once, the same way a shared
secret does. It is exactly as trustworthy as the process that launched it.

**Two modes, one tool surface.** Set `AGENTCO_REGISTRY_URL` (with
`AGENTCO_SECRET`) and every tool proxies to that registry over HTTP instead of
opening files. Without it, nothing changes and the stores are local.

The remote mode is not a convenience. On the default backend the stores are a
SQLite file and two JSONL files under an OS lock — single-host by construction
— so a harness that is not on the registry's filesystem could previously push
a scope claim through `publish.py` and never pull a work item at all. It also
means a machine running this server in remote mode must NOT also be pointed at
the same paths locally: two writers is the blindness the registry exists to
remove. With `AGENTCO_DB` set (agentco/stores.py) the work queue and the SOP
library are tables in the registry database, and the local-write restriction
lifts — several processes on the one host may hold it at once. What remote
mode still buys is the machine boundary: a database file is not shared by
being named from another host.

In remote mode the identity is checked, because now there is a signature: the
actor is whoever the shared secret authenticates as, and the registry ignores
any name in the body.
"""

from __future__ import annotations

import json
import os
from typing import Optional

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from agentco import auth, db, events as events_module, leases, policy, scope, snapshots
from agentco.errors import Refusal
from agentco.keys import NaturalKeyError
# publish.py is standard-library only, so importing it here costs the `mcp`
# extra nothing — unlike `app`, which would drag FastAPI in (see above).
from agentco.publish import Registry, RegistryError
from agentco.refusals import classify
from agentco.sop import DEFAULT_SOP_STORE, SOP_STORE_ENV_VAR, SopError, SopLibrary, resolve_sop_store
from agentco.stores import open_queue, open_sop_library, resolve_registry_db
from agentco.work import (
    DEFAULT_LEASE_TTL_S,
    DEFAULT_WORK_STORE,
    WORK_STORE_ENV_VAR,
    CapabilityError,
    Queue,
    TERMINAL,
    WorkError,
    WorkStatus,
    resolve_work_store,
)

DB_ENV_VAR = "AGENTCO_REGISTRY_DB"
DEFAULT_DB = "registry.sqlite3"

ACTOR_ENV_VAR = "AGENTCO_ACTOR"
# Which harness is speaking, self-reported and never verified. Over stdio
# NOTHING here is verified — the actor itself is only as trustworthy as the
# process that set it — so the label adds no new weakness locally. It exists
# so that the same field is populated on both transports, and so a digest can
# say "cursor" instead of leaving the reader to guess from the actor name.
AGENT_LABEL_ENV_VAR = "AGENTCO_AGENT_LABEL"
# A generic default, same posture as app.py's DEFAULT_OPERATOR = "operator" —
# it works out of the box for a single harness and is the first thing a
# multi-harness deployment overrides in its own `.mcp.json` entry.
DEFAULT_ACTOR = "mcp-actor"

REGISTRY_URL_ENV_VAR = "AGENTCO_REGISTRY_URL"
SECRET_ENV_VAR = "AGENTCO_SECRET"

CAPABILITIES_ENV_VAR = "AGENTCO_CAPABILITIES"


def resolve_db_path(path: Optional[str] = None) -> str:
    return resolve_registry_db(path, DB_ENV_VAR, DEFAULT_DB)


def resolve_actor(actor: Optional[str] = None) -> str:
    return actor or os.environ.get(ACTOR_ENV_VAR) or DEFAULT_ACTOR


def resolve_agent_label(label: Optional[str] = None) -> Optional[str]:
    """The self-reported harness name, or None. Unset is None, never a guess.

    There is no default. Inferring the harness from the process tree would be a
    guess rendered beside authenticated data, and a plausible wrong label is
    worse than an absent one — absent is legible.
    """
    return auth.normalise_agent_label(label or os.environ.get(AGENT_LABEL_ENV_VAR))


def resolve_registry_url(base_url: Optional[str] = None) -> Optional[str]:
    """The one switch between the two modes. `None` means local files."""
    return base_url or os.environ.get(REGISTRY_URL_ENV_VAR) or None


def resolve_secret(secret: Optional[str] = None) -> Optional[str]:
    return secret or os.environ.get(SECRET_ENV_VAR) or None


def resolve_capabilities(capabilities: Optional[list[str]] = None) -> list[str]:
    """What this worker can run, declared once in config rather than per call.

    A capability the model has to remember to pass is one it will eventually
    forget, and the failure is silent in the wrong direction: `claim()` fails
    CLOSED, so a forgotten declaration means the machine that is the only one
    able to do a job quietly stops being offered it. Declaring the lane in
    `.mcp.json` — beside the actor, which is the same kind of assertion — makes
    it a property of the deployment instead of of the conversation.
    """
    if capabilities is not None:
        return list(capabilities)
    raw = os.environ.get(CAPABILITIES_ENV_VAR) or ""
    return [part.strip() for part in raw.split(",") if part.strip()]


def _refuse(exc: Exception) -> ToolError:
    """The one seam every tool raises through, so a refusal is never a bare 500.

    `Refusal.__str__` already renders `"{code}: {message} — {remediation}"`
    (agentco/errors.py), so forwarding it verbatim keeps both halves intact
    for whatever harness is on the other end of the stdio pipe. `WorkError`
    and `NaturalKeyError` predate `Refusal` and carry no code of their own, so
    they pass through `agentco.refusals.classify` — the same mapping the HTTP
    surface uses — and arrive as `work_conflict: ...`, `sop_refused: ...` and
    so on. Before that, the same wrong input read as a code on one transport
    and as prose on another, which is exactly the drift the conformance suite
    exists to catch. The library's own words are kept; only the code is added.
    """
    return ToolError(str(classify(exc)))


class _LocalBackend:
    """The original behaviour: open the stores and call the core directly."""

    remote = False

    def __init__(self, conn, queue: Queue, library: SopLibrary, actor: str, db_path: str,
                 agent_label: Optional[str] = None):
        self.conn, self.queue, self.library, self.actor = conn, queue, library, actor
        self.agent_label = agent_label
        self._db_path = db_path

    def claim_scope(self, **kw) -> dict:
        return leases.claim(self.conn, actor=self.actor, agent_label=self.agent_label, **kw)

    def release_scope(self, lease_uid: str, action: str) -> dict:
        return leases.release(self.conn, actor=self.actor, lease_uid=lease_uid, action=action,
                              agent_label=self.agent_label)

    def snapshot(self, **kw) -> dict:
        return snapshots.take(self.conn, actor=self.actor, agent_label=self.agent_label, **kw)

    def events(self, **kw) -> dict:
        return events_module.read(self.conn, **kw)

    def work_pull(self, agent: str, capabilities, ttl_seconds: int) -> Optional[dict]:
        for item in self.queue.ready(agent=agent):
            try:
                claimed = self.queue.claim(item.id, agent, ttl_seconds=ttl_seconds, capabilities=capabilities)
            except CapabilityError:
                continue
            if claimed is not None:
                return json.loads(claimed.to_json())
        return None

    def work_report(self, item_id: str, attempt: int, status: WorkStatus, **kw) -> Optional[dict]:
        updated = self.queue.report_result(item_id, attempt, status, **kw)
        return json.loads(updated.to_json()) if updated is not None else None

    def work_create(self, title: str, **kw) -> dict:
        return json.loads(self.queue.create(title, **kw).to_json())

    def attest(self, item_id: str, attestation: dict, submitted_by: str,
               capabilities=None, adjudication=None) -> Optional[dict]:
        updated = self.queue.attest(
            item_id, attestation, submitted_by=submitted_by, capabilities=capabilities,
            adjudication=adjudication,
        )
        return json.loads(updated.to_json()) if updated is not None else None

    def sop_get(self, sop_id: str, version: Optional[int]) -> Optional[dict]:
        sop = self.library.get(sop_id, version=version)
        return json.loads(sop.to_json()) if sop is not None else None

    def sop_revise(self, sop_id: str, changes: dict, title: Optional[str],
                   author: str, author_kind: str) -> dict:
        sop = self.library.revise(sop_id, title=title, author=author, author_kind=author_kind, **changes)
        return json.loads(sop.to_json())

    def sop_activate(self, sop_id: str, version: int, author: str, author_kind: str) -> dict:
        sop = self.library.activate(sop_id, version, author=author, author_kind=author_kind)
        return json.loads(sop.to_json())

    def describe(self) -> dict:
        return {
            "mode": "local",
            "stores": {
                "registryDb": self._db_path,
                "workStore": str(self.queue.path),
                "sopStore": str(self.library.path),
            },
        }


class _RemoteBackend:
    """The same nine operations against a registry over HTTP.

    Every method here exists to unwrap one envelope. The HTTP surface answers
    `{"state": ..., "item": ...}` because a poller needs to tell "nothing to do"
    from "here it is"; the MCP contract is the bare item or `None`, and has been
    since before there was a remote mode. Translating in one place keeps that
    promise rather than changing what a tool returns depending on how the server
    happens to be configured — which no calling model could be expected to know.
    """

    remote = True

    def __init__(self, registry: Registry):
        self.registry = registry
        self.actor = registry.actor

    def claim_scope(self, repo, prefixes, intent, holder=None, ttl_seconds=None) -> dict:
        # `holder` is dropped deliberately: over HTTP the registry records
        # `holderAttested` against the AUTHENTICATED actor, and passing an
        # unverified name through would be the one case where the two encodings
        # disagree about who made a claim.
        return self.registry.claim_scope(repo, list(prefixes), intent, ttl_seconds=ttl_seconds)

    def release_scope(self, lease_uid: str, action: str) -> dict:
        return self.registry.release_scope(lease_uid, action)

    def snapshot(self, artifact_uri: str, purpose: str, ttl_days=None) -> dict:
        return self.registry.snapshot(artifact_uri, purpose)

    def events(self, since=None, limit=events_module.DEFAULT_LIMIT, kind=None) -> dict:
        return self.registry.events(since=since, limit=limit)

    def work_pull(self, agent: str, capabilities, ttl_seconds: int) -> Optional[dict]:
        pulled = self.registry.work_pull(capabilities=capabilities, ttl_seconds=ttl_seconds)
        return pulled.get("item") if pulled.get("state") == "leased" else None

    def work_report(self, item_id: str, attempt: int, status: WorkStatus, result=None,
                    idempotency_key=None, attestation=None, submitted_by=None) -> Optional[dict]:
        # `submitted_by` is deliberately NOT forwarded. Over HTTP the actor comes
        # from the signature and the server derives it; sending it would be a
        # body claiming an identity, which is the one thing every transport here
        # refuses.
        return self.registry.work_report(
            item_id, attempt, status.value, result=result,
            idempotency_key=idempotency_key, attestation=attestation,
        ).get("item")

    def attest(self, item_id: str, attestation: dict, submitted_by: str,
               capabilities=None, adjudication=None) -> Optional[dict]:
        return self.registry.attest(
            item_id, attestation, capabilities=list(capabilities or ()) or None,
            adjudication=adjudication,
        ).get("item")

    def work_create(self, title: str, requires=(), blocked_by=(), assigned_agent=None,
                    natural_key=None, source=None, source_id=None, kind=None,
                    subject=None, period=None, metadata=None, verify=None) -> dict:
        payload = {
            "requires": list(requires), "blockedBy": list(blocked_by),
            "assignedAgent": assigned_agent, "naturalKey": natural_key,
            "source": source, "sourceId": source_id, "kind": kind,
            "subject": subject, "period": period, "metadata": metadata,
            "verify": verify,
        }
        return self.registry.work_create(
            title, **{k: v for k, v in payload.items() if v not in (None, [], ())}
        )["item"]

    def sop_get(self, sop_id: str, version: Optional[int]) -> Optional[dict]:
        return self.registry.sop_get(sop_id, version=version).get("sop")

    def sop_revise(self, sop_id: str, changes: dict, title: Optional[str],
                   author: str, author_kind: str) -> dict:
        # `author` and `author_kind` are dropped deliberately: over HTTP the
        # registry takes the author from the signature and the kind from ITS
        # declaration of who is human. A proxy that forwarded its own reading
        # would be the one place a caller could become human by configuration.
        body = dict(changes)
        if title is not None:
            body["title"] = title
        return self.registry.sop_revise(sop_id, **body)["sop"]

    def sop_activate(self, sop_id: str, version: int, author: str, author_kind: str) -> dict:
        return self.registry.sop_activate(sop_id, version)["sop"]

    def describe(self) -> dict:
        return {"mode": "remote", "registryUrl": self.registry.base_url}


def create_server(
    db_path: Optional[str] = None,
    work_store: Optional[str] = None,
    sop_store: Optional[str] = None,
    actor: Optional[str] = None,
    base_url: Optional[str] = None,
    secret: Optional[str] = None,
    registry: Optional[Registry] = None,
    agent_label: Optional[str] = None,
) -> FastMCP:
    """Build the server. Paths/actor/registry are injectable so tests need no env vars.

    One `conn`/`queue`/`library` per server instance in local mode, closed over
    by every tool below — the same "one connection for the process" posture as
    `app.create_app`, and for the same reason: this is a single-node stage-1
    surface, not a pool of anything.

    In remote mode none of those are opened at all. That is the point: a
    process that is proxying to a registry must not also hold the stores, or it
    becomes the second writer.
    """
    who = resolve_actor(actor)
    url = resolve_registry_url(base_url)

    if registry is not None:
        backend: _LocalBackend | _RemoteBackend = _RemoteBackend(registry)
        who = registry.actor
    elif url:
        key = resolve_secret(secret)
        if not key:
            # Loud, and at construction. Falling back to local files here would
            # be the worst available failure: the harness would appear to work
            # while writing to a store nobody else can see, and the first
            # symptom would be a queue that is permanently empty on one machine.
            raise Refusal(
                code="secret_required",
                message=f"{REGISTRY_URL_ENV_VAR} is set but no shared secret is",
                remediation=(
                    f"Set {SECRET_ENV_VAR} to the secret minted for {who!r} "
                    f"(`python3 -m agentco keygen {who}`), or unset "
                    f"{REGISTRY_URL_ENV_VAR} to use local stores."
                ),
                http_status=400,
            )
        backend = _RemoteBackend(Registry(who, key, url))
    else:
        backend = _LocalBackend(
            db.connect(resolve_db_path(db_path)),
            open_queue(work_store),
            open_sop_library(sop_store),
            who,
            resolve_db_path(db_path),
            resolve_agent_label(agent_label),
        )

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
            return backend.claim_scope(
                repo=repo, prefixes=prefixes, intent=intent,
                holder=holder, ttl_seconds=ttl_seconds,
            )
        except (Refusal, RegistryError) as exc:
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
            return backend.release_scope(lease_uid, action)
        except (Refusal, RegistryError) as exc:
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
            return backend.snapshot(artifact_uri=artifact_uri, purpose=purpose, ttl_days=ttl_days)
        except (Refusal, RegistryError) as exc:
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
            return backend.events(since=since, limit=limit, kind=kind)
        except (Refusal, RegistryError) as exc:
            raise _refuse(exc) from exc

    @mcp.tool(name="work_pull")
    def work_pull(ttl_seconds: int = DEFAULT_LEASE_TTL_S) -> Optional[dict]:
        """Claim the next ready work item this identity can run, with a fenced lease.

        Claims as THIS NODE — never as a name the model supplies. An earlier
        version took `agent` as a tool argument, and the lease holder is what a
        judged gate's separation check compares the verifier against, so a model
        could pull as "ghost-worker", report, and then attest its own work from
        the same node and pass. Identity is configuration (`AGENTCO_ACTOR`), and
        so are capabilities (`AGENTCO_CAPABILITIES`): a model is in no position
        to declare, per call, who the machine it runs on is or what it is set up
        to do.

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
        try:
            return backend.work_pull(backend.actor, resolve_capabilities(), ttl_seconds)
        except (Refusal, WorkError, RegistryError) as exc:
            raise _refuse(exc) from exc

    @mcp.tool(name="work_report")
    def work_report(
        item_id: str,
        attempt: int,
        status: str,
        result: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        attestation: Optional[dict] = None,
    ) -> dict:
        """Report a terminal outcome (done/failed), fenced on the lease attempt it was issued under.

        A mismatch means this item was handed to someone else while the work
        was in flight — the report is refused because the work was
        SUPERSEDED, not lost, and accepting it would overwrite whoever holds
        the item now. A missing item is refused too, rather than answered
        with a bare success a caller could read as "recorded, nothing to say".

        On a GATED item, `done` is a request rather than an outcome. A
        deterministic gate requires `attestation` in the same call — the check
        you re-ran, its exit status, the environment, and when — and the report
        is refused without it. A judged or human gate takes no attestation here
        at all and parks the item as `awaiting_verify` for somebody else to
        answer with `attest`.
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
            updated = backend.work_report(
                item_id, attempt, parsed_status, result=result,
                idempotency_key=idempotency_key, attestation=attestation,
                submitted_by=who,
            )
        except (WorkError, ValueError, Refusal, RegistryError) as exc:
            raise _refuse(exc) from exc
        if updated is None:
            raise ToolError(
                f"no work item {item_id!r} on this queue — there is nothing to fence "
                f"this report against. Check the id came from work_pull or work_create "
                f"against this same store."
            )
        return updated

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
        verify: Optional[dict] = None,
    ) -> dict:
        """File a new work item. A duplicate natural key returns the EXISTING item, not an error.

        Natural-key precedence is explicit > generated > external (agentco/keys.py):
        pass `natural_key` yourself, or all of `kind`+`subject`+`period` for
        recurring generated work, or `source`+`source_id` to mirror an
        external record. A partial key is refused before anything is written,
        because a repaired key would be a silent duplicate or a silent merge.

        `verify` attaches a gate: `{kind, check, max_park_seconds, on_timeout}`,
        kind being `deterministic` (the completing process re-runs the check and
        reports an attestation), `judged` (routed to somebody who is not the
        executor) or `human`. A malformed gate is refused here rather than
        stored, because a gate that silently does nothing still reports green.
        Omit it and the item behaves exactly as items did before gates existed.
        """
        try:
            item = backend.work_create(
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
                verify=verify,
            )
        except (WorkError, NaturalKeyError, Refusal, RegistryError) as exc:
            raise _refuse(exc) from exc
        return item

    @mcp.tool(name="sop_get")
    def sop_get(sop_id: str, version: Optional[int] = None) -> Optional[dict]:
        """Read one SOP version, or the active version when `version` is omitted.

        `None` is the normal answer for an unknown id, or one with no active
        version yet — resolving a pin must never fail loudly, or an SOP that
        has since been superseded would become unreadable to every instance
        still pinned to the version it was created under.
        """
        try:
            return backend.sop_get(sop_id, version)
        except (Refusal, RegistryError) as exc:
            raise _refuse(exc) from exc

    @mcp.tool(name="sop_revise")
    def sop_revise(sop_id: str, changes: dict, title: Optional[str] = None) -> dict:
        """Write the next version of a procedure as a DRAFT; the old one stays readable.

        `changes` holds SOP fields (purpose, definition_of_done, common_mistakes,
        executor, tags, ...); unset fields carry forward, `null` clears one.
        Nothing is promoted until `sop_activate`. The revision policy applies:
        as an agent you cannot touch a `money`/`irreversible` step, demote a
        human step, or undo a change a human made.
        """
        try:
            return backend.sop_revise(
                sop_id, changes, title,
                author=who, author_kind=policy.kind_of(who, policy.humans_from_env()),
            )
        except (SopError, ValueError, Refusal, RegistryError) as exc:
            raise _refuse(exc) from exc

    @mcp.tool(name="sop_activate")
    def sop_activate(sop_id: str, version: int) -> dict:
        """Make one version the one every reader gets. Policed like a revision."""
        try:
            return backend.sop_activate(
                sop_id, version,
                author=who, author_kind=policy.kind_of(who, policy.humans_from_env()),
            )
        except (SopError, ValueError, Refusal, RegistryError) as exc:
            raise _refuse(exc) from exc

    @mcp.tool(name="attest")
    def attest(item_id: str, attestation: dict, adjudication: Optional[dict] = None) -> dict:
        """Answer a gate on an item parked in `awaiting_verify` (or re-run a failed one).

        The verifier's verb, as distinct from the executor's `work_report`: a
        judged gate exists precisely because the party that did the work may not
        be the party that says it is correct, and that separation is enforced
        rather than documented.

        `attestation` is `{check, exit_status, environment, at}` — the check
        that ran, what it returned, where, and when. Exit 0 closes the item;
        anything else lands `verify_failed`, which keeps blocking everything
        downstream until THIS item's own gate passes. A fix filed beside it
        never substitutes for it.

        The plane verifies the record's shape and stores the claim. It never
        runs the command, which makes this a trust floor rather than a proof.

        `adjudication` — optional `{verdict: good|bad, evidence}` — tags the
        divergence you saw while verifying. You may not be the executor.
        """
        try:
            updated = backend.attest(
                item_id,
                attestation,
                submitted_by=who,
                adjudication=adjudication,
                # The NODE's declared capabilities, from its own configuration —
                # not an argument on the tool. A model that could pass this
                # would be declaring, per call, what the machine it runs on is
                # set up to do, which is a claim it is in no position to make.
                capabilities=resolve_capabilities(),
            )
        except (WorkError, ValueError, Refusal, RegistryError) as exc:
            raise _refuse(exc) from exc
        if updated is None:
            raise ToolError(
                f"no work item {item_id!r} on this queue — there is nothing here "
                f"to attest against. Check the id came from work_pull or "
                f"work_create against this same store."
            )
        return updated

    @mcp.tool(name="whoami")
    def whoami() -> dict:
        """What this actor is identified as, and which registry or stores this server is pointed at.

        There is no RBAC yet — every actor can call every tool (stage 2, gated
        on the adoption gate: docs/roadmap.md) — so this exists for a harness's
        first call to confirm its own `.mcp.json` configuration landed on the
        identity and stores it expects, before it stakes a claim or files work
        under the wrong name.

        `mode` is the field to read first. A harness that believes it is on a
        shared registry while quietly writing to a local file sees a queue that
        is simply always empty, and nothing about that looks like an error.
        """
        return {
            "actor": backend.actor,
            "enforcement": "advisory",
            # Advisory applies to SCOPE claims. The capability gate is the one
            # thing here that does refuse, and it fails closed — so a harness
            # must be able to see what it has declared without pulling anything.
            "capabilities": resolve_capabilities(),
            # Whether the `verify` capability above means anything: bound to the
            # operator's declared verifiers, or self-asserted.
            "verifiers": sorted(policy.verifiers_from_env()) or "undeclared (verify is self-asserted)",
            **backend.describe(),
            "scope": {"minSegments": scope.MIN_SEGMENTS, "intents": list(scope.INTENTS)},
            "eventKinds": list(events_module.KINDS),
        }

    return mcp


def main() -> None:
    create_server().run(transport="stdio")


if __name__ == "__main__":
    main()
