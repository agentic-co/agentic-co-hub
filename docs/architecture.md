# Architecture

> **Status: outline.** This document states the shape and the invariants. Sections marked
> *(planned)* are designed but not extracted yet — see [roadmap](roadmap.md).

## The problem, stated precisely

An organisation has N people running M agent harnesses, where no two harnesses are the
same and nobody controls all of them. They already share one anchor: a system of record
(Azure DevOps, Jira, Linear, GitHub Issues). The system of record is not inadequate — the
layer *above* it does not exist:

1. **Nobody holds the objectives** the organisation just decided to run on, in a form an
   agent can read.
2. **Nobody can see** that two agents are working the same directory.
3. **Nobody can tell** that a document moved after someone built against it.
4. **No escalation from any harness has a delivery guarantee.** An agent that asks a
   human a question and gets no answer has failed silently, in someone else's system.

AgentCo is the smallest thing that holds exactly those four.

## Boundaries — what the plane owns

The plane is authoritative for exactly three classes of thing:

- **Objectives** — because nothing else holds them.
- **Coordination claims** — the assertions people and agents make about each other's
  work: who is in which directory, what version something was built against.
- **Its own audit** — the record of decisions it made.

Everything else is a **cache**, carrying `observed_at` and `source`. Dropping every cache
must change no answer the plane gives. That test is what stops a coordination layer
becoming a second system of record by accretion.

> **The law:** never hold a competing version of a fact another system owns.

## Primitives

### `ScopeClaim` — the concurrency primitive

An advisory declaration: *"I am working in these directories, with this intent."*

Scope is `(repo, path-prefix set)` at **directory granularity** — prefixes, not globs. A
prefix must name at least `k` segments below the repo root (`k = 2` initially), so a claim
on `src/` is refused with a remediation naming the requirement.

This specificity rule is load-bearing rather than fussy. Ten people each holding a claim
on `src/` is the *default* outcome if nothing refuses it; every claim then intersects
every other, conflicts fire constantly, and within days everyone learns the registry is
noise. Making concurrency visible is most of the value and none of the political cost —
**but only if the visibility is precise.**

Intersection is segment-wise prefix overlap computed as a set operation, so `src/budget`
and `src/budgeting` do **not** collide. A conflict fires only between two *different*
holders and carries **both intents**, so *prototype vs implement* reads differently from
*implement vs implement*.

The registry publishes its own precision: conflicts fired ÷ conflicts acted on. Below a
floor, the granularity rule is wrong and the fix is `k`, not more claims.

### `Snapshot` — a pointer, never a copy

*"I am working from this version of that."* One call records a URI plus a cheap version
token — a git SHA, a content hash, a document eTag. **The body is never stored**, which is
asserted against the database file's raw bytes rather than through the API.

*Never fetched* is the intent and is not currently true in one case: `resolve_https` sends
a HEAD request, but urllib re-issues a redirect as GET, so a redirected pointer transfers
the body it then discards. Open defect, failing test against it. Nothing is written either
way — the storage claim holds unconditionally.

This is the invariant most worth defending, because violating it is how a coordination
layer quietly becomes the document store it promised not to be.

### `DivergenceObserved` — at the cadence boundary, deliberately

When a snapshotted pointer's version token changes, that is recorded and **accumulated**.
It is delivered at a cadence boundary — a daily batch, a weekly review — never in real
time.

The gating *is* the product. Real-time notifications on every document change are exactly
what the people who need this are already drowning in.

Honest limits, stated because they decide the value: it covers only artifacts somebody
snapshotted (**no snapshot, no signal**); it needs a stable URI plus a cheap version
token, so a local file on somebody's laptop has nothing and no design fixes that; and it
detects *that* something moved, not *whether it matters*. Deciding that is the human's
job.

### The change feed

A totally-ordered, authenticated, replayable log. Cursor-based, **not** timestamp-based:
the cursor is opaque, monotonic and resumable, so a harness that has been offline for a
month gets everything it missed rather than a window.

Opacity is deliberate. A timestamp cursor loses events written inside one clock tick and
invites clients to do arithmetic on it, which turns a resumable feed into a windowed query
— and from then on, changing the storage is a breaking change.

### Work coordination *(partially extracted)*

A queue with a **fenced lease protocol**: compare-and-swap claim plus a fencing token, so
a worker whose lease expired cannot report results over the work that replaced it. One
uniqueness rule on the ingest path, so no source can invent its own idempotency mechanism.

## Storage

Three backends, one set of interfaces. JSONL under an advisory file lock is the default;
`AGENTCO_DB=<path>` selects SQLite — one file, or `AGENTCO_DB=postgresql://...`/`postgres://...`
selects Postgres — one database ([roadmap](roadmap.md#where-the-stores-live) has the
resolution table and the reasoning).

The protocol does not change between any of them, and that is enforced rather than
intended: the lease logic has exactly one implementation, and both database-backed stores
inherit it, overriding only how a row is read and written. Under SQLite the compare-and-swap
is `BEGIN IMMEDIATE` — the write lock is taken *before* the read the write is conditioned on
— followed by an update conditioned on the attempt it was decided against. A read, then a
write, with the transaction opened around only the second half, is the shape that passes
every single-process test and fails the moment a second machine pulls the same queue.

Postgres gets there by a different route rather than by copying SQLite's lock: a thin
connection adapter (`agentco/pgadapter.py`) presents the same `sqlite3.Connection` surface
this codebase's SQL is written against — `?` placeholders, `with conn:`, `cur.lastrowid` —
over a real `psycopg` connection, so none of `events.py`/`leases.py`/`snapshots.py`/
`metrics.py`/`divergence.py`/`sqlstore.py` needed a second, PG-dialect copy of a single
query. What the adapter does NOT paper over is `BEGIN IMMEDIATE`'s whole-database lock
itself — Postgres has no such lock, and does not need one: the fenced claim's own
compare-and-swap `WHERE` clause, plus `SELECT ... FOR UPDATE` on the one contested row, and
`pg_advisory_xact_lock` on a brand-new natural key with no row yet to lock, carry the same
correctness under Postgres's READ COMMITTED that the whole-database lock buys for free
under SQLite. `sqlstore.py`'s docstrings on `SqlQueue._mutate` and `.create` say which
primitive and why, at the two places it actually matters.

Schema changes go through numbered migrations recorded in the database itself, applied
once and one transaction each, so every database is at version N or at N-1 and never halfway
through. Migration 1 is the schema that already exists in deployed registries, written to be
safe to apply to them — a migration system that only works on empty files is one nobody can
adopt. Where SQLite's DDL is not portable (`AUTOINCREMENT`, `INSERT OR IGNORE`), that one
migration carries a Postgres-dialect rewrite (`GENERATED ALWAYS AS IDENTITY`, `ON CONFLICT
... DO NOTHING`) alongside it; every other migration's DDL already runs unchanged on both.

### `ASOP` — a procedure that is a versioned sequence of gated steps

A standard operating procedure is an object, not a block of text pasted into
every item that follows it. Since **ASOP v3** (ratified 2026-09-04,
[`packages/asop/ASOP.md`](../packages/asop/ASOP.md)) the object is a versioned,
ordered sequence of **steps**, and a step is what earlier versions called the
procedure.

Two things follow from that grain, and they are the whole of what v3 changed.

**A run is a tree.** `run(asop_id, inputs, bindings)` files a parent work item
pinned to `(asop_id, version)` carrying the run's inputs, plus one child per
step pinned to `(asop_id, version, step)`. Each child carries a COPY of its
step's text, its gate, and `blocked_by` taken from the step's `after` — so a
plan-vs-actual review reads the words the executor was actually handed even
after the procedure moves on, and a parent cannot close while a step is open.
Filing goes through the same parent/child path everything else uses, so the
decomposition bounds (7 children, 3 deep) apply to a run tree as they do to a
goal.

**The gate is on the step, authored with the version.** Before v3 the record
had no gate and whoever filed the work supplied one. Where the filer sits on
the executor's side — the ordinary case for a single-operator organisation —
that is the executor's own side authoring the check it will be graded by,
which is the failure the contract exists to prevent. So a run supplies no gate
and is **refused** if it passes one.

**Bindings come from the caller.** An ASOP names *roles* — `implementer`,
`validator` — never agents, which is what makes the artefact shareable: two
organisations run the same version with different agents and their outcomes
are still counted against the same thing. Which agent fills a role is a fact
about a harness's own roster, so the plane never invents one. A role with no
binding refuses (`role_unbound`), bindings that break a `distinct` constraint
refuse (`constraint_unsatisfiable`), and so does a judged step bound to the
same actor as the step it judges — the one constraint the contract requires
whether or not the author wrote it down.

**A template is not an instance.** An ASOP never enters the queue — if it did
it would be claimable, and a template that can be completed is a bug.

Every run refusal fires before anything is filed, so a refused run leaves the
queue byte-identical: a draft or retired version (`sop_refused`), a missing
declared input (`inputs_missing`), an unbound role, an unsatisfiable
constraint, and a tree that would break the decomposition bounds.

Pinning is what makes the procedure evaluable at all: a run that referenced
"the ASOP" rather than "v3" would attribute its outcome to text that has since
changed. `outcomes_by_version()` groups runs by version **and by step**,
reporting **counts** rather than a bare success rate — a rate is gameable in
both directions, since a procedure applied to progressively harder cases looks
like it is degrading, and failures re-filed as new runs look like improvement.
In-flight work counts as neither outcome, and a version with nothing finished
reports `None`, never `0`. The per-step rows are the reason the grain moved:
"did the rewrite of step 2 help" is a comparison of two step rows, and before
v3 there was no such row.

That pin is the same relationship a snapshot has to a document, so the same
question applies: `drifted()` reports in-flight runs whose procedure has moved.
It reports and never migrates — re-pointing running work at a newer procedure
changes the job under whoever is doing it.

**A step may be another ASOP.** `uses: {asop_id, version}` files the inner
procedure's tree as that step's children, pinned to the inner version. Depth
counts against the three-deep bound, checked while the run is planned rather
than half-way through filing it. Sequencing *between* procedures is
deliberately not here: that is orchestration, and the contract puts it in the
harness (§11.4). `next_sop` and the chain walk it fed were dropped with v3.

#### Lifecycle

Drafting a revision does not promote it. The version in use stays in use until
it is explicitly activated, so writing an improvement is a safe act rather than
one that quietly takes the live procedure out of service. `retire` withdraws a
version with no successor: no new runs file from it, runs already in flight
finish under their pin, and the record is kept forever — a retired version that
stopped resolving would make every outcome counted against it unreadable.

Revision and activation are policed when the reviser is an agent
(`agentco/policy.py`), now per step. A step tagged `money` or `irreversible`
is frozen against agents, and an agent may not *remove* one either — deleting
a step is the one edit that leaves nothing behind for a rule to protect. A
step's class ratchets toward human only, where "human" means its role's kind
or its gate's kind is human, and removing a human step is the same demotion by
deletion. No agent revision moves a field into a state a human moved it away
from. Who is human is what the operator declared in `AGENTCO_HUMANS` — never
inferred, and an undeclared registry polices everyone.

Two verbs are human-only outright, because there is no proposed version to
compare and so no diff a rule could read: **`retire`**, and **`promote`**.

#### The improvement loop

It exists now, and it is per step. A divergence is **adjudicated** by a party
that is not the executor: `good` feeds that step's `proposals`, `bad` feeds
that step's `common_mistakes` (capped at three per step — the cap is the
discipline). `propose()` drafts the next version from the adjudications nobody
has consumed, and a step that diverged more than once in one pass also earns a
structural proposal on the sequence, because the same divergence recurring at
the same boundary is evidence for a split or a reorder rather than another
line of prose.

Who may adjudicate is **declared**, like who may verify — `AGENTCO_ADJUDICATORS`
alongside `AGENTCO_VERIFIERS`. The two defaults point opposite ways on purpose.
An undeclared verifier set fails open, because the alternative is every judged
gate resolving on its clock, which is work approved by a timer. An undeclared
adjudicator set fails **closed** — only declared humans adjudicate — because
what degrades when an agent grades the loop that revises the procedures it
follows is not throughput but the evidence base, and nobody notices.

**`promote(run_id)`** is the front door: a completed run tree becomes a draft
ASOP, beads to steps, executors to roles, `blocked_by` to `after`. It is
refused when an active ASOP already covers the run's `task_type` — two
procedures for one type of task means outcomes counted in two places and
compared in neither, and the path for a variant is a new *version*. Human-only
in v3; agents may draft revisions, a person decides that a shape is a
procedure.

#### Legacy records

Rows written before v3 are upgraded, never dropped. Migration 0009 adds an
`asops` table and copies every `sops` row forward as a one-step ASOP; the
`sops` table itself is left byte-identical as provenance. The JSONL store
applies the same upgrade on read, because a file store has no migration runner
to hang one off. A v2 record carried no gate, so the upgrade has to choose
one, and it fails **closed** to a `human` gate: a deterministic gate would
assert that the record's `validation` prose is a command that exits 0, which
nothing ever checked, and a judged gate would route to a route nobody
declared. The blast radius is small by construction — only a v2 record that
was ACTIVE at the moment of the upgrade can be run at all — and one human
revision replaces it with the real gate.

### Review routing *(planned)*

A decision reaches a **named** human, with an acknowledgement, a claim deadline, an
explicit defer reason, and escalation to a role when unclaimed. An offer transfers
execution, never responsibility: the offering side keeps the finalizer, and work cannot
reach a terminal state until the accepting side publishes a terminal attestation or the
offer expires and returns.

A cross-boundary handoff into the void is the failure this exists to close.

## Integration surface

Two encodings over **one semantic core**: plain HTTP, and MCP for harnesses that speak it.
A third encoding is added when it ships, not before.

A conformance suite testing both encodings against the core is *planned and does not exist*
— see the roadmap. Until it does, the two surfaces are kept honest by construction rather
than by test: every MCP tool is a thin wrapper calling the same function the HTTP handler
calls.

Authentication is per-actor HMAC to start — one line of config, no identity-provider
consent required — with OIDC as the path for organisations that want it. **The actor is
always taken from the token, never from the payload.**

That is narrower than it sounds and the difference matters. The *actor* — who made the
call — cannot be forged. The `holder` field of a scope claim is a separate, payload-supplied
value, so a caller can file a lease naming a colleague. The lease records that as
`holderAttested`, but **the conflict record other people actually read does not currently
carry that flag**, so a conflict raised by an unverified claim is presently
indistinguishable from a real one. That is a known open defect with a failing test against
it, not a property of the design.

## Connectors

Everything organisation-specific is a connector, and connectors are configuration:

- **Systems of record** — Azure DevOps, Jira, Linear, GitHub Issues *(planned)*
- **Delivery** — Teams, Slack, email *(planned)*
- **Identity** — OIDC providers *(planned)*

Naming Azure DevOps is fine; it is a product thousands of organisations run. Naming *your*
organisation's instance is configuration and does not belong in this repository.

## The ASOP contract package

The gate schema (`deterministic` / `judged` / `human`, with attestation) and
the record shape (`SopStatus`, the `ASOP` and `Step` dataclasses,
`validate_asop`, `validate_step`, and the legacy `SOP`/`validate_fields` kept
readable for the upgrade) live in `packages/asop/` — a separate distribution, `agentco-asop` (import
name `asop`), inside this repo but published independently of it. `Refusal`,
the one exception type the contract speaks in, moved with them.

The reason is the AgentCo Harness: a standalone execution runtime in a
separate repository, extracted from the same private implementation this
plane was, that grew its own gate schema under a different name for the same
axis (`class` instead of `kind`) with its own optional fields (a staged
`checks` ladder, `cwd`/`timeout_s`/`rubric`/`judge_route`). Two schemas
answering the same three-way split — deterministic re-run, judged by a
different route, addressed to a human — is not a divergence either side
chose; it is two extractions from one system that had not yet had to agree
with each other. `asop.gates.validate_gate` is that agreement: one normalised
shape, with the one thing that actually differs between callers — whether
the park-clock group is mandatory — exposed as an argument (`require`)
rather than hidden as an assumption.

This plane imports `asop` as an ordinary dependency (`[tool.uv.sources]` in
the root `pyproject.toml` resolves it from `packages/asop/`, editable, via a
`uv` workspace). `agentco/gates.py`, `agentco/errors.py` and `agentco/sop.py`
are now thin shims: they call into `asop.gates` / `asop.errors` / `asop.sop`
with this plane's own calling convention (`require=("clock",)`, its own
`MAX_PARK_SECONDS` ceiling) and re-export what callers in this repo already
expect, so nothing outside those three files had to change.
`agentco.errors.Refusal is asop.errors.Refusal` holds by construction — the
plane does not define its own `Refusal`, it imports the shared one — which
is what lets a `try/except Refusal` written against either side catch a
refusal raised by the other.

What did **not** move: `SopLibrary` (the versioned store — drafting,
revising, activating, instantiating work items, grouping outcomes by
version), file locking, and the revision policy. Those are about how a
procedure is KEPT, which is plane policy, not part of what a procedure IS.
Likewise `Unauthenticated` (an HTTP-transport concern) and `scope_too_broad`
(a `ScopeClaim` concern) stay in `agentco/errors.py` — a harness adopting
the ASOP contract has no reason to know what a scope claim is.

## Failure semantics

**Fail static.** If the plane is unavailable, nothing is blocked. Every tool falls back to
what it does today, and the plane reconciles on restart. This is true by construction while
the plane is advisory-only; the stage that introduces actuation makes it false, and that
stage requires a second operator before it ships.

## Deliberately not doing

- Becoming the place work lives.
- Blocking anybody's work.
- Holding document bodies.
- Writing to a system of record without an explicit, separately-gated path.
- Measuring individual people's throughput. A finding a person cannot dismiss is
  surveillance; a dismissal with a reason is data.
