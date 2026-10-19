# Roadmap

> Honest about what exists. Nothing below is marked done because it was designed.

## Now — extraction

AgentCo grew up as a private implementation inside one organisation's context. The
coordination primitives are built and tested there; this repository is the extraction of
the parts that are not about any particular company.

The extraction principle: **anything that names a company is configuration, not code.**
The seam turned out to be cleaner than expected — the coordination-layer packages carry
almost no organisation-specific references, and the coupling that does exist concentrated
in exactly the place it should have, the connectors.

| Component | Built | Extracted | Notes |
|---|:--:|:--:|---|
| `leakguard` — the guard that keeps this repo publishable | ✅ | ✅ | Runs in CI and pre-commit |
| Scope claims + conflict detection | ✅ | ✅ | Prefix model, precision self-audit |
| Snapshots + divergence digest | ✅ | ✅ | No body ever stored; cadence-boundary delivery; pluggable resolvers |
| Change feed (opaque resumable cursor) | ✅ | ✅ | |
| HMAC authentication | ✅ | ✅ | Actor from token, never payload |
| Adoption metrics | ✅ | ✅ | Weekly publishers, time-to-first-event, per-verb latency, L1→L2 conversion (null until there is an L1 publisher, never 0) |
| Work queue + fenced leases | ✅ | ✅ | CAS + fencing token, **proven across 12 real processes** |
| Idempotency (one uniqueness rule on ingest) | ✅ | ✅ | Loud duplicate suppression |
| ASOPs as versioned sequences of gated steps (v3) | 🆕 | ✅ | The ASOP is the sequence, a `Step` is what v2 called the procedure, and the gate is on the step — authored with the version, refused if a filer supplies one. `run()` files a TREE (parent pinned `(asop_id, version)`, one bead per step pinned `(asop_id, version, step)`, `after` carried as `blocked_by`); outcomes are grouped per version AND per step. See [`docs/decisions/0003-asop-v3-adoption.md`](decisions/0003-asop-v3-adoption.md) |
| Run refusals, all before anything is filed | 🆕 | ✅ | `sop_refused` (draft or retired), `inputs_missing`, `role_unbound`, `constraint_unsatisfiable` — the last also covering a judged step bound to the actor it judges. `role_unbound` also covers a binding the registry has no key for: an actor that cannot authenticate can never pull, so filing it is filing into a void. Bindings come from the CALLER; the plane never invents one |
| Stranded steps surfaced by the pulse | 🆕 | ✅ | A step ready past its actor's cadence (a day, undeclared) that nobody has pulled is an `attention` finding naming the actor and the wait. The key file cannot catch this one — an actor can authenticate and still never run — and a run nobody pulls otherwise looks exactly like an empty queue |
| `retired` status | 🆕 | ✅ | Human-only. No new runs file; in-flight runs finish under their pin; the record is kept forever, or every outcome counted against it becomes unreadable |
| Nested procedures (`uses`) | 🆕 | ✅ | A step may be another ASOP, filed as that step's children pinned to the inner version. Depth checked while the run is PLANNED, so a run that would break the bound files nothing. `next_sop` and the chain walk are gone with v3 §11.4 — sequencing between procedures is the harness's |
| A run closes when its steps do (§5.5) | 🆕 | ✅ | The write that lands a step's last `done` closes its container in the SAME transaction and writes the run-level plan-vs-actual from the per-step reviews; only `done` counts, so a parked or failed gate holds the run open. Nested containers close bottom-up. Without it `outcomes_by_version` counted every finished run as in-flight. `pulse --apply` repairs stranded containers by calling the same function |
| `promote(run_id)` | 🆕 | ✅ | A completed run tree becomes a draft ASOP: beads to steps, executors to roles, `blocked_by` to `after`. Human-only in v3; refused when an active ASOP already covers the run's `task_type` |
| Declared adjudicators (`AGENTCO_ADJUDICATORS`) | 🆕 | ✅ | A route may judge a divergence only if the operator declared it. Undeclared fails CLOSED (humans only) — the opposite default from verifiers, because what degrades when it fails open is the evidence base rather than throughput |
| Legacy v2 records upgraded, never dropped | 🆕 | ✅ | Migration 0009 adds `asops` and copies every `sops` row forward as a one-step ASOP, leaving `sops` byte-identical; the JSONL store applies the same upgrade on read. A v2 record had no gate, so the upgrade fails CLOSED to a `human` gate |
| ASOP contract package (`asop-spec`, `asop-spec`) | 🆕 | ✅ | The gate schema, `Refusal`, and the SOP record shape split out of `agentco/gates.py` \| `errors.py` \| `sop.py` into a standalone package a harness can depend on without depending on this plane; `agentco/{gates,errors,sop}.py` are now thin shims over it. Merges this plane's park-clock gate fields with the AgentCo Harness's staged-check/runtime-hint fields into one normalised schema. Floor is `asop-spec>=0.4.0`: from 0.4 a judged or human attestation must carry `verdict: {passed, reason}` and a negative verdict fails the gate even at exit 0 |
| `asop-spec` 0.4 → 0.6 compatibility (spec v3.5, v3.6) | 🆕 | ✅ | The pin is `>=0.4.0,<1` and the lock is 0.5.0. Migration 0011 adds the seven chaining columns; the store reads only what the installed contract models and quarantines what it cannot. Before this the plane did not import on 0.5.0 (the column guard fired), and `revise` on the JSONL store silently dropped every chaining field. Matrix: [`docs/asop.md`](asop.md#contract-compatibility). |
| Request bodies: strict and lenient routes, field provenance, weekly asop-spec watch | 🆕 | ✅ | Unknown keys refused `422 unknown_field` where a dropped key can remove a safety property, reported as `ignored` (and counted in `/metrics`) everywhere else; every route declared, pinned by a test. Messages name the closest key and whether a newer released asop-spec has the field, from a bundled generated table (`tools/asop_fields.py`). A weekly job opens one issue when PyPI has a release adding fields the hub does not forward: [`docs/wire.md`](wire.md) |
| Run ownership (ASOP §7.2) and the administrative transfer | 🆕 | ✅ | One owner store per run recorded at filing (`AGENTCO_HUB_ID`, `AGENTCO_STORES`), `not_the_owner` for anyone else, a pack reference (uri + sha256, never content), and `POST /runs/{id}/transfer` for a declared human admin (`AGENTCO_ADMINS`, new): epoch bump, history, voided leases, `RunTransferred`; the pulse names orphaned runs: [`docs/asop.md`](asop.md#run-ownership-and-the-administrative-transfer-asop-72-owner-decision-2026-10-18) |
| Change-feed retention floor (R6) | 🆕 | ✅ | `agentco prune-events` / `AGENTCO_EVENT_RETENTION` delete a contiguous prefix and record the floor; `GET /events` carries `retainedFrom`; a cursor older than the floor is `410 cursor_expired` with the resync remedy, never a silent gap; the pulse reports the floor: [`docs/architecture.md`](architecture.md#the-change-feed) |
| Lease renew (R1) | 🆕 | ✅ | `POST /work/{id}/renew {attempt, ttlSeconds}` for the current fence holder only; extends the expiry without moving the fence or touching the repair budget; capped at `AGENTCO_LEASE_MAX_AGE` (default 24h, unmeasured); the pulse shows lease ages. No MCP tool until ADR 0004 decides a thirteenth: [`docs/architecture.md`](architecture.md#work-coordination-partially-extracted) |
| Chain manifests, adoptions, approvals (spec v3.6 §3.7, §3.10) | 🆕 | ✅ | `agentco/chains.py`, migration 0012. A manifest's members are checked against this store's versions and digests (`GET /sops/{id}` serves `digest`); adoptions and approvals are declared-human only, signed, and the adoption impact is computed. A chain run files at its pinned version or is refused `adoption_required`. Needs asop-spec 0.6.0. Not built: the chain outcome record and step journal, which need journal ingest — see [`docs/asop.md`](asop.md#chain-manifests-adoptions-and-approvals-v36-agentcochainspy) |
| Shared memory: item store (ADR 0006 §1, slice M1) | 🆕 | ✅ | **Off by default** (`AGENTCO_MEMORY=on` to enable; off = every `/memory` route `404 memory_disabled`, no store opened). `agentco/memory.py`, `agentco/clearance.py`, migration 0013. Owner-labeled items in held layers, one authoritative hub each, read-only replicas, required provenance, pinned class, audit, nothing deleted; JSONL/SQLite/Postgres. Clearance per data owner (hub ∧ actor) on the memory routes. The wall on every other read path, decay and residuals are later slices: [`docs/memory.md`](memory.md) |
| Shared memory: the wall (ADR 0006 §2, slice M2) | 🆕 | ✅ | Clearance applied before every read path: list, search, item routes (walled answers like missing), the change feed (`MemoryChanged` carries the label, never content, filtered per reader), the MCP `events` tool, internal feed readers (none), the pulse count. Known metadata/timing/existence limits listed and pinned by tests: [`docs/memory.md`](memory.md#known-limits--open-documented-not-claimed-closed) |
| Shared memory: usefulness and decay (ADR 0006 §4-§5, slice M3) | 🆕 | ✅ | Pure engine `agentco/memory_engine.py` on an exposure clock; credit only for items invoked in a trace, against an outcome the plane derives from the work item's gate; power-law decay with a floor; pinned exemption with calendar review; cold storage, demand pull, replay, probation, reconsolidation; nothing deleted. Every parameter is configuration labelled NOT VALIDATED: [`docs/memory.md`](memory.md#built-usefulness-and-decay-m3) |
| Durable storage backend (SQLite) | 🆕 | ✅ | Opt-in via `AGENTCO_DB`; same interfaces, conformance-tested against the JSONL default |
| Durable storage backend (Postgres) | 🆕 | ✅ | Opt-in via `AGENTCO_DB=postgresql://…`; thin `sqlite3.Connection`-shaped adapter (`agentco/pgadapter.py`) over `psycopg`, no second copy of the SQL; same conformance suite, `psycopg[binary]` is an optional `postgres` extra |
| `tools/migrate_sqlite_to_pg.py` | 🆕 | ✅ | Idempotent cutover from a stopped SQLite file to Postgres; preserves `events.seq`/`calls.id`/`conflict_actions.id` and advances the identity sequence so a subscriber's cursor keeps resuming |
| Numbered schema migrations | 🆕 | ✅ | Applied once, one transaction each, recorded in the file |
| Silent-schedule audit | ✅ | ✅ | `agentco pulse`: an actor silent past its declared cadence (`AGENTCO_CADENCE`) is a finding; undeclared is `null`, never a guess — and the pulse judges its own gap first. Reservations-style scheduling of work itself is still ⏳ |
| Usage metering across harnesses | ✅ | ⏳ | Unreported is `null`, never `0` |
| Health checks with consequence classes | ✅ | ✅ | `agentco pulse` — exit code is the worst class (ok / attention / fatal), never a count; dry-run by default, `--apply` runs the sweeps nobody was running (expired leases, park clocks, quarantine) and records a `PulseObserved` heartbeat — see [`docs/pulse.md`](pulse.md) |
| MCP surface | ✅ | ✅ | 12 tools of a stated 12 — the ceiling is binding; a byte budget over the schemas is published beside the count. **v3 puts seven verbs on the wire the surface does not carry, and the byte budget is now at its limit (12,496 of 12,500): a decision, not an oversight — [`docs/decisions/0004-mcp-surface-under-asop-v3.md`](decisions/0004-mcp-surface-under-asop-v3.md), proposed, undecided** |
| Tier-1 context injection (shared repo file) | ✅ | ✅ | Byte-level splice, CRLF-safe, idempotent |
| Session-hook injection (tier 3) | ✅ | ✅ | Fail-open per dependency; byte-identical uninstall |
| Outbox + drainer + receipts (L1) | 🆕 | ✅ | `.agentco/outbox.jsonl` plus `agentco drain`; appending a line needs no package on the writer's side. The tier-1 splice carries the instructions, the tier-3 session block carries the receipts — see [`docs/outbox.md`](outbox.md) |
| Gates + attestation (`verify` payload, `awaiting_verify` / `verify_failed`) | 🆕 | ✅ | Validated at the write boundary; neither status releases `blocked_by` — proven across processes by a real poller; the plane stores the claim and never runs the check |
| Judged/human verifiers (L3) — routing, park clocks, quarantine digest | 🆕 | ✅ | `verify` counts only for `AGENTCO_VERIFIERS` once declared; a human gate names who answers it; a default is never a verdict |
| Revision policy | 🆕 | ✅ | Per STEP since v3. A protected-tagged step must be gated `human` at authoring — refused by the record for the default tags, and plane-side for any a registry adds, since the contract cannot enforce a set it was never told. Protected tags freeze a step against agents ABSOLUTELY — an agent may not revise or activate a procedure containing one, deletion included, because changing what runs around a `money` step changes what that step does; class ratchets toward human only (role kind or gate kind), and removing a human step is the same demotion by deletion; on a FIRST activation the ratchet is absolute too, since baseline and target are the same version and every differential check would be vacuous; no undoing a human. A fourth rule, `human_only`, covers `retire` and `promote`. HTTP 403 `revision_policy:<rule>`, proven by mutation |
| Adjudication + plan-vs-actual + revision proposals | 🆕 | ✅ | Per STEP since v3: a `good` adjudication feeds that step's `proposals`, a `bad` one that step's `common_mistakes` (capped at three per step), and a step that diverged twice in one pass earns a structural proposal on the sequence. Adjudicator ≠ executor enforced on every transport, and who may adjudicate at all is declared; `metadata.plan_vs_actual` written per step at completion; `agentco lessons --propose` drafts, never activates |
| Decomposition bounds enforced at create | 🆕 | ✅ | Seven children, three deep, repair beside; HTTP 422 `decomposition_bound` |
| Write-back to an external record | 🆕 | ✅ | Opt-in, notice-only, off until configured — see [`docs/writeback.md`](writeback.md) |
| Conformance suite (`agentco conform --level`) | 🆕 | ✅ | One semantic core, four transports; found six drifts on its first two runs, all fixed |
| ASOP eval harness (`evals/`) | 🆕 | ✅ | Two claims, two kinds of evidence; lesson provenance tells a loop-fed lesson from a hand-fed one. Not shipped in the wheel |

### How AgentCo reaches a harness at all

Nothing can push into a model's context — it is assembled by the harness at turn
boundaries. So reaching one is always: write into a file it already reads, give
the model a reason to ask, or tell the human. Four tiers, ordered by what they
cost the harness's owner:

| Tier | Mechanism | Owner effort |
|---|---|---|
| **1 — the repo** | A managed block spliced into `CLAUDE.md` / `AGENTS.md`, refreshed on a schedule | **zero** |
| **2 — MCP** | `events` since a cursor, and the rest of the tool surface | one config line |
| **3 — session hook** | Fetch-and-inject at session start | small, owner-written |
| **4 — humans** | A digest to chat; the person forwards what matters | manual, and honest about it |

Tier 1 is the only one that reaches a harness nobody configured, which is the
premise of the whole project. It is also the one that has to be careful: it
edits a file in somebody else's repository. So the splice reads and writes
**bytes**, detects the file's own line-ending convention and matches it, never
touches a byte outside its markers, never creates a file that does not exist,
and is dry-run by default.

That care is not hypothetical. In the implementation this was extracted from,
the same module normalised every `\r\n` in the target file to `\n` and persisted
it — one render re-encoded a whole file, far outside the managed block. It shipped
green, because every test fixture was authored with `write_text()`, which cannot
produce a CRLF file at all.

These four tiers are all the plane reaching the harness. The opposite
direction — a harness nobody configured reaching the plane — had no floor
until the outbox: everything a harness could *do*, as opposed to read, sat
behind tier 2's one config line. The
[participation-ladder ADR](decisions/0002-participation-ladder.md) names that
gap **L1** and closes it the same way tier 1 closes the read side — a file the
harness already has permission to touch, this time written rather than read.
See [`docs/outbox.md`](outbox.md).

**Tier-1 content is repo-scoped, never per-person.** Live scope claims belong in
a shared file because they are meant to be public — that is the point of the
registry. Divergence on your own snapshots does not: the target is a file the
whole team reads and most repos commit, so personal state placed there is
published to everybody, permanently, through version control. That content goes
to the individual through tier 3 instead.

### Connecting a harness

One entry in the harness's `.mcp.json`, and nothing about how that harness works
changes:

```json
{
  "mcpServers": {
    "agentco": {
      "command": "python3",
      "args": ["-m", "agentco", "serve-mcp"],
      "env": {
        "AGENTCO_ACTOR": "your-name",
        "AGENTCO_WORK_STORE": "/path/to/work.jsonl",
        "AGENTCO_SOP_STORE": "/path/to/sops.jsonl",
        "AGENTCO_REGISTRY_DB": "/path/to/registry.sqlite3"
      }
    }
  }
}
```

### Where the stores live

Three backends behind one set of interfaces, chosen by one variable:

| `AGENTCO_DB` | Work queue | SOP library | Registry |
|---|---|---|---|
| unset *(default)* | `work.jsonl` | `sops.jsonl` | `AGENTCO_REGISTRY_DB` |
| `/path/to/agentco.sqlite3` | that file | that file | that file, unless `AGENTCO_REGISTRY_DB` overrides |
| `postgresql://…` or `postgres://…` | that database | that database | that database, unless `AGENTCO_REGISTRY_DB` overrides |

Unset is the default deliberately. JSONL under an advisory lock is greppable
at 02:00, diffs in review, and lets one corrupt line be quarantined instead of
taking the store with it — worth more than durability to one person running one
harness, and that person has to be able to use this before anyone else can.

`AGENTCO_DB` is what a team turns on when more than one process opens the same
store. It buys real transactions (the fenced claim is `BEGIN IMMEDIATE` plus a
conditional update, not a read followed by a write), a unique index enforcing
the idempotency rule rather than a scan that assumes a lock, and numbered
migrations so a schema change has somewhere to go. `AGENTCO_REGISTRY_DB` still
wins where it is already set, because turning on the durable backend must not
silently relocate a registry that already exists.

A `postgresql://`/`postgres://` value is the same opt-in, one step further: a
managed database instead of a file a team has to put somewhere and back up
itself. `agentco/pgadapter.py` is a thin connection adapter, not a second
dialect of the SQL — the query text in `events.py`/`leases.py`/`snapshots.py`/
`metrics.py`/`divergence.py`/`sqlstore.py` is unchanged from the SQLite path.
Install the optional `postgres` extra (`pip install 'agentco[postgres]'`) to
pull in `psycopg`; the default install stays dependency-free. Cutting an
existing SQLite deployment over: stop the writer, copy the file (never point
anything at a live one), run `tools/migrate_sqlite_to_pg.py`, then flip
`AGENTCO_DB` to the Postgres DSN.

The two backends are held to one contract by running the same behavioural
tests against both — `tests/conftest.py` parametrises the queue and library
fixtures, so every work-queue and ASOP test is a conformance test. A contract
proven against one implementation is a description of that implementation.

Twelve tools, and twelve is a ceiling enforced by a test rather than
remembered. All twelve are built: `claim_scope`, `release_scope`, `snapshot`,
`events`, `work_pull`, `work_report`, `work_create`, `sop_get`, `whoami`,
`attest`, and — with Phase 4, behind the revision policy — `sop_revise` and
`sop_activate`, the two names `docs/decisions/0002-participation-ladder.md`
reserved before either shipped.
A large tool surface costs every calling harness context on every
tool-choice decision it makes — that is paid by every conversation, not just
the one using it. A thirteenth tool means deleting one. The count is a proxy
for that cost, and the cost is bytes, not names, so a byte budget over the
registered tools' schemas is published alongside the count
(`tests/test_mcp_server.py`) and fails if it grows while the count holds.

**That revisit condition has now fired.** ASOP v3 added seven verbs to the
HTTP surface and the schemas sit at 12,496 of 12,500 bytes with the count
still at twelve, so there is no room for a thirteenth tool at any description
length — the v3 tool docstrings were written down to fit rather than the
budget being raised. What to do about it is
[`docs/decisions/0004-mcp-surface-under-asop-v3.md`](decisions/0004-mcp-surface-under-asop-v3.md),
which is **proposed and undecided**: the surface stays at twelve until it is
settled.

Over stdio there is no request to sign, so the actor is whatever `AGENTCO_ACTOR`
asserts at process start — exactly as trustworthy as the process that launched
it. Over HTTP the actor comes from an HMAC signature instead, never the payload.

### Extension points, so connectors never need a core change

Two registries, both following the same shape:

- `snapshots.register_resolver(scheme, fn)` — teach the registry to read one
  URI scheme's version token. A resolver must obtain that token **without
  fetching the body**; a connector that downloads a document to hash it has
  broken the invariant, and no test in core can catch that on its behalf.
- `delivery.register_sender(name, fn)` — deliver a digest somewhere, with
  native formatting. The built-in sender posts plain JSON to a configured URL,
  which is the honest default: a coordination layer has no business knowing
  what any particular chat vendor's card format is.

## Next — the core

- ~~HTTP surface over one semantic core, with a conformance suite.~~ Built — `agentco/conformance.py`, `agentco conform --level`.
- Identity beyond HMAC (OIDC), for organisations that want it.
- Change-feed subscriptions with webhook auth.
- The lease proof catching up with the protocol: attempt-advance on report and reap,
  the reaper's in-lock liveness re-check and `assigned_agent` enforcement are
  single-process tested only (see below).

### Hub network — planned, not built

Accepted as a design in [`decisions/0007-hub-network-graph-and-channels.md`](decisions/0007-hub-network-graph-and-channels.md),
extending the built push-only digest of [0005](decisions/0005-federated-hub-topology.md):

- Many connections per hub instead of one parent URL; cycle protection for
  forwarded work.
- Declared channels per connection (work, answers, memory, notices), enforced
  on both ends.
- Signed per-owner clearance that only narrows per hop; the existing digest
  filtered by it.
- Pull-based work: stimulus by age and priority, thresholds, a Decline verb,
  evidence pointers, WIP limits, random audit, a level-skipping alert path.
- Harness-to-human binding through an external identity provider, after a
  review of delegated-authentication patterns (this is the "identity beyond
  HMAC" item above, given a shape).

Proposed for one topology in [`decisions/0008-control-loop-personal-and-company-hubs.md`](decisions/0008-control-loop-personal-and-company-hubs.md):
one person working for several companies, pull first, one partition per company, answers as
signed envelopes (the person's per-company key, verified by its public half). Not built. The
envelope is defined in this hub as the v1 contract, to be proposed to asop-spec 0.7.0 as an
additive change with no change to 0.6.x semantics (Decision 1 = A, 2026-10-19).

Still open (owner decision 8, 2026-10-18): the **identity mechanism** — direction accepted, mechanism
not chosen, confidence 0.55; revisit when a second organisation needs to connect a hub — and **credit
across hubs** — not decided, no internal market; revisit when two hubs exchange work with different
owners.

### A dedicated `cannot_evaluate` status — designed, not built (option B)

**Today (option A, 2026-10-18):** a could-not-evaluate gate result lands `verify_failed` with
`metadata.verify_cannot_evaluate.pending`, is not charged, and is re-run (`docs/asop.md`, "Could not
evaluate"). **Option B** gives it its own stored work-item status, `cannot_evaluate` — the name the
spec's §5.2 table already uses for the bead.

**Why it is cleaner.** The case becomes a fact of the status rather than a flag a reader must know to
look for: `?status=cannot_evaluate` filters it, every consumer that switches on status handles it by
construction, and "failed" stops meaning two different things depending on a metadata key. A runtime
or second implementation reading the plane's items would not need this plane's flag to tell a failed
check from a check that could not run.

**What it would touch** — the reason it is deferred:
- `WorkStatus` (work.py) and `releases_blockers` (must stay DONE-only), `TERMINAL`, the verify-state
  guard in `report_result`, `attest` (the transition out), `ready()` / `claim()` / `rerun_pending`;
- `SqlQueue._ready_candidates` (its `status IN (...)` list) and `idx_work_status`; there are **no** SQL
  `CHECK` constraints on `status` on SQLite or Postgres today, so no constraint migration — but every
  backend's readers matter (below);
- `verifiers`: `route_open_gates`, `sweep_park_clocks`, `sweep_quarantine`, `quarantine_digest`,
  `verifier_status`;
- the pulse: housekeeping facts, `check_stranded_steps`, `close_finished_runs`;
- the digest and write-back texts, and the feed kinds that announce verify states;
- `SopLibrary.outcomes_by_version` (the `cannotEvaluate` column would read the status), run and step
  views;
- `metrics.py` wherever it groups by status;
- the HTTP API: `parse_terminal_status`, `GET /work?status=` (its `unknown_status` refusal lists the
  legal set — a strict client validating against that enum sees a new value);
- the MCP tools' descriptions and the remote relay, which pass statuses through;
- `agentco conform` scenarios and `conformance.py`'s status expectations.

**Compatibility for old clients and old binaries.** An older plane reading a row with a status value it
cannot model **quarantines** it (work.py, "a newer writer's unknown status value") — safe, but the item
disappears from that reader's queue. A strict client that validates `status` against today's enum
breaks on the new value. So: readers first, writers later, and the flag kept.

**Migration plan.**
1. Release N: every reader understands `cannot_evaluate` (treats it exactly as option A treats
   `verify_failed` + `pending`); nothing writes it. Option A's flag stays authoritative.
2. Release N+1, behind a setting: writers land `cannot_evaluate`, and still write
   `verify_cannot_evaluate` with `pending: true`, so a client reading only the flag keeps working.
3. Additive data migration: `verify_failed` with `pending: true` → `cannot_evaluate` (SQL `UPDATE`
   in a numbered migration; JSONL on the next write), with the reverse statement recorded for rollback.
4. A later major may drop the flag's `pending` field once no supported client reads it.

**Revisit condition.** When a runtime or a second implementation needs to tell "failed" from "could not
evaluate" without reading this plane's metadata flag; when the spec's conformance suite asserts the bead
status `cannot_evaluate`; or when a status-keyed consumer is found mishandling `verify_failed` +
`pending` — whichever comes first.

## Later — review delivery

The routing spine: named owners, shared queues, offers with claim deadlines, explicit
defer reasons, escalation, and a terminal `Undeliverable` state. Capacity modelling ships
as **measurement** — observed and published — and throttles nothing until there is real
data to throttle on.

### Shared memory — planned, not built

Accepted as a design in [`decisions/0006-hub-memory-design.md`](decisions/0006-hub-memory-design.md).
The item store, the wall and the usefulness engine are built **behind `AGENTCO_MEMORY`, off by
default** (the rows above; [`docs/memory.md`](memory.md)). Owner decision 8 (2026-10-18):
the three-valued verified outcome and holding shared-layer records in full are **confirmed**; every
parameter value is **open** until real traces measure it. The original list, in dependency order:

- An owner-labeled memory item, stored beside the ASOP store, with one
  authoritative hub per item.
- Label enforcement in retrieval, before ranking, tested to fail on bypass.
- Per-owner partition and session isolation as a connector requirement.
- Action-trace reporting from harnesses, bound to a three-valued verified outcome.
- Exposure clock, power-law decay with an activation floor, a pinned class with
  calendar review; cold storage with demand-pull revival, periodic replay and
  probation.
- The residuals path: owner categories, never-leave list, mechanical checker,
  blind validator, per-owner aggregate check, batch approval, reversible
  category graduation.
- The lesson-to-ASOP-revision hand-off through the existing improvement loop.
- A primary-source spot-check of the research behind the design, before any
  parameter is claimed.

### Borrowed candidates (from the 2026-08-31 OpenViking review)

Three patterns from [OpenViking](https://github.com/volcengine/OpenViking) worth weighing
when their sections come up — recorded here so the attribution survives:

- **Tiered object delivery (L0 abstract → L1 overview → L2 full)** for change-feed events
  and SOP objects, so a Tier-1 CLAUDE.md splice stays cheap at scale.
- **A uniform, deterministic addressing scheme** for snapshot URIs (their `viking://`
  filesystem convention) — browsable and debuggable beats opaque strings.
- **Session-end auto-extraction** as the shape of the improvement loop. *Since
  adopted, in a different shape:* the loop is adjudication → `plan_vs_actual` →
  `agentco lessons --propose`, driven by a deliberate pass rather than by session end.
- *(from the LifeOS review, same date)* **Definition-of-done at `work_create`** — LifeOS's
  ISA discipline articulates the ideal state before work starts. *Since adopted:* the
  `verify` payload on `work_create` is that definition, validated at the write boundary.
- *(LifeOS)* **`pai-freshness-v1`-style frontmatter** (`last_updated`/`last_reviewed`) as
  cheap staleness metadata for SOPs and docs, complementing Snapshot's artifact staleness.
- *(LifeOS)* **Tiered effort routing** as the template for review-routing capacity
  modelling — match decision urgency to responder tier.

## Not planned

Writing to a system of record, beyond the one path that now exists and was built to the
constraint this section always stated: separately gated, narrow, and off by default. The
[write-back connector](writeback.md) notifies an originating record that its human gate
is parked — a notice, never a state change — and does nothing at all until an operator
configures a destination. The whole value proposition is that adopting AgentCo cannot
damage the system you already trust, and a notification path that can only append is the
largest exception that leaves that true.

---

## What has been proven, and what has only been tested

A distinction worth keeping, because they are not the same evidence.

**Proven under adversarial conditions:**

- **The lease protocol holds across real OS processes.** Twelve spawn-mode
  processes, barrier-synced onto one item: exactly one wins, the store stays
  intact, a stale holder is fenced out, and concurrent creates with one natural
  key converge on a single row. Verified by mutation — removing the advisory
  lock fails the storm and dedup tests, disabling the fence fails the stale-holder
  test. A test that cannot fail when the mechanism is removed proves nothing, so
  that check is the actual evidence.
- **Snapshots never store a document body**, asserted by scanning the database
  file's raw bytes rather than the API.
- **`leakguard` catches real leakage** — it found thirteen genuine leaks in this
  repository's own authoring before the first commit.

**Reviewed adversarially.** That review has happened. A second party, instructed
to refute rather than confirm and to read the code without reading the tests,
produced twenty-four findings across six load-bearing claims — five of the six
did not survive. Seventeen are fixed; the rest carry failing tests naming the
property that should hold. `tests/test_adversarial_findings.py` is written by
that reviewer, structured as counterexamples rather than confirmations, and each
regression test in it was verified to FAIL against the pre-fix code before its
marker came off.

The single most transferable finding was not a defect but a pattern: six existing
tests asserted the exact property their code lacked, by testing the half that
held. A test that cannot fail when the mechanism it guards is removed proves
nothing, and looks identical in the summary line to one that can.

**Run across real hubs, on a schedule.** `tests/network/` brings up five hubs
from the real image and a Postgres on one Docker network. It checks federation
as built (ADR 0005, push-only), including a wrong secret, an undeclared child, a
`docker network disconnect` partition with a parent restart during it, and the
rejoin. A feed reader on the parent resumes on its cursor and sees every digest
exactly once. The rig also covers the memory switch on and off in different
hubs, with the wall holding over the wire and across a restart. It runs a run
ownership transfer across a key revoked by the real `agentco revoke`, and the
live chain proof (22 checks) on SQLite and on Postgres. It is not in the main
suite. `.github/workflows/network.yml` runs it on a path filter (the package,
the image, the lock, the rig), nightly, and on demand. Locally:
`AGENTCO_RIG=1 uv run --extra dev --extra server --extra mcp pytest tests/network -v`.
Its first run found that memory switched on in the image could not write its
store.

**Tested but not proven:** the rest. Most of the suite remains hermetic and
single-process, and much of it is still written by the same author as the code.

One boundary worth stating precisely, because the honest version is narrower than
the flattering one: the lease protocol's twelve-process proof still holds — both
mutations were re-run against current code and both still fail the right tests —
but **the protocol has grown faster than the proof**. Attempt-advance on report,
attempt-advance on reap, the reaper's in-lock liveness re-check and
`assigned_agent` enforcement are single-process tested only.

The gate closed part of that gap rather than widening it. Derived blockedness is
now proven across processes: eight workers race to complete a gated item while a
ninth does nothing but poll `ready()` thousands of times, and the dependent never
once appears — the momentarily-done race observed from outside, by a real poller,
which is the only place a window that opens and closes between two writes can be
seen at all. Alongside it: a refusal raised inside the lock leaves the row
claimable and unchanged; a reaped worker's report is fenced out even when it
carries a PASSING attestation, because evidence is not authority; and of six
concurrent verifiers exactly one can close a gate, with the stored evidence
belonging to the one that did.

## Known issues

Eleven open defects — eight with a failing test naming the property that should
hold, three recorded without one yet: [`docs/known-issues.md`](known-issues.md).
None of them lose work or report a wrong result as a right one — those were fixed.

## The adoption gate

The project's own falsification criterion, written before the result is known:

> **Two identities other than the author publishing weekly, for four consecutive weeks.**

Not "have tried it" — that measures politeness. Four consecutive weeks measures use. A
missed week resets the streak rather than bridging it, and the current week never counts
while it is still running.

**Corrected 2026-09-23: "identities" was the wrong unit, and the gate now counts
PEOPLE.** The sentence above is left as written because a falsification criterion that
gets quietly reworded is not one. At two or three agents per person, one adopter holding
a laptop key and two agent keys satisfied "two identities other than the author" by
himself, every week, and the report named three actors that nobody reading it could tell
were one person. The key table's `owner` field is what makes the unit available; a
person's agents now count once, for them, and the operator's own agents are excluded with
him rather than counting as publishers other than the operator.

A criterion revised after the clock has started deserves suspicion, so the direction is
stated plainly: this can only make the gate HARDER. Every party is one or more
identities, so a party count is never larger than the identity count it replaces, and no
week that fails under the new rule would have failed under the old one.

If that fails, the honest reading is that coordination across independently-owned
harnesses is not a problem other people have, and this stops at the primitives that are
useful on their own. Stars are the metric that will be available and they are not the one
being used.
