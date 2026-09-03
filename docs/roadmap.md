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
| SOPs as versioned templates | 🆕 | ✅ | Pinned per instance; outcomes grouped by version |
| ASOP contract package (`agentco-asop`, `packages/asop/`) | 🆕 | ✅ | The gate schema, `Refusal`, and the SOP record shape split out of `agentco/gates.py` \| `errors.py` \| `sop.py` into a standalone package a harness can depend on without depending on this plane; `agentco/{gates,errors,sop}.py` are now thin shims over it. Merges this plane's park-clock gate fields with the AgentCo Harness's staged-check/runtime-hint fields into one normalised schema |
| Durable storage backend (SQLite) | 🆕 | ✅ | Opt-in via `AGENTCO_DB`; same interfaces, conformance-tested against the JSONL default |
| Durable storage backend (Postgres) | 🆕 | ✅ | Opt-in via `AGENTCO_DB=postgresql://…`; thin `sqlite3.Connection`-shaped adapter (`agentco/pgadapter.py`) over `psycopg`, no second copy of the SQL; same conformance suite, `psycopg[binary]` is an optional `postgres` extra |
| `tools/migrate_sqlite_to_pg.py` | 🆕 | ✅ | Idempotent cutover from a stopped SQLite file to Postgres; preserves `events.seq`/`calls.id`/`conflict_actions.id` and advances the identity sequence so a subscriber's cursor keeps resuming |
| Numbered schema migrations | 🆕 | ✅ | Applied once, one transaction each, recorded in the file |
| Silent-schedule audit | ✅ | ✅ | `agentco pulse`: an actor silent past its declared cadence (`AGENTCO_CADENCE`) is a finding; undeclared is `null`, never a guess — and the pulse judges its own gap first. Reservations-style scheduling of work itself is still ⏳ |
| Usage metering across harnesses | ✅ | ⏳ | Unreported is `null`, never `0` |
| Health checks with consequence classes | ✅ | ✅ | `agentco pulse` — exit code is the worst class (ok / attention / fatal), never a count; dry-run by default, `--apply` runs the sweeps nobody was running (expired leases, park clocks, quarantine) and records a `PulseObserved` heartbeat — see [`docs/pulse.md`](pulse.md) |
| MCP surface | ✅ | ✅ | 12 tools of a stated 12 — the ceiling is binding; a byte budget over the schemas is published beside the count |
| Tier-1 context injection (shared repo file) | ✅ | ✅ | Byte-level splice, CRLF-safe, idempotent |
| Session-hook injection (tier 3) | ✅ | ✅ | Fail-open per dependency; byte-identical uninstall |
| Outbox + drainer + receipts (L1) | 🆕 | ✅ | `.agentco/outbox.jsonl` plus `agentco drain`; appending a line needs no package on the writer's side. The tier-1 splice carries the instructions, the tier-3 session block carries the receipts — see [`docs/outbox.md`](outbox.md) |
| Gates + attestation (`verify` payload, `awaiting_verify` / `verify_failed`) | 🆕 | ✅ | Validated at the write boundary; neither status releases `blocked_by` — proven across processes by a real poller; the plane stores the claim and never runs the check |
| Judged/human verifiers (L3) — routing, park clocks, quarantine digest | 🆕 | ✅ | `verify` counts only for `AGENTCO_VERIFIERS` once declared; a human gate names who answers it; a default is never a verdict |
| Revision policy | 🆕 | ✅ | Protected tags freeze a step against agents; class ratchets toward human only; no undoing a human. HTTP 403 `revision_policy:<rule>`, proven by mutation |
| Adjudication + plan-vs-actual + revision proposals | 🆕 | ✅ | Adjudicator ≠ executor enforced on every transport; `metadata.plan_vs_actual` written at completion; `agentco lessons --propose` drafts, never activates |
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
fixtures, so every work-queue and SOP test is a conformance test. A contract
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

## Later — review delivery

The routing spine: named owners, shared queues, offers with claim deadlines, explicit
defer reasons, escalation, and a terminal `Undeliverable` state. Capacity modelling ships
as **measurement** — observed and published — and throttles nothing until there is real
data to throttle on.

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

If that fails, the honest reading is that coordination across independently-owned
harnesses is not a problem other people have, and this stops at the primitives that are
useful on their own. Stars are the metric that will be available and they are not the one
being used.
