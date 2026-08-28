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
token — a git SHA, a content hash, a document eTag. **The body is never fetched or
stored.**

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

### Review routing *(planned)*

A decision reaches a **named** human, with an acknowledgement, a claim deadline, an
explicit defer reason, and escalation to a role when unclaimed. An offer transfers
execution, never responsibility: the offering side keeps the finalizer, and work cannot
reach a terminal state until the accepting side publishes a terminal attestation or the
offer expires and returns.

A cross-boundary handoff into the void is the failure this exists to close.

## Integration surface

Two encodings over **one semantic core**: plain HTTP, and MCP for harnesses that speak it.
Conformance is tested against the core and HTTP; a third encoding is added when it ships,
not before.

Authentication is per-actor HMAC to start — one line of config, no identity-provider
consent required — with OIDC as the path for organisations that want it. **The actor is
always taken from the token, never from the payload**, so no client can file a claim in
another person's name.

## Connectors

Everything organisation-specific is a connector, and connectors are configuration:

- **Systems of record** — Azure DevOps, Jira, Linear, GitHub Issues *(planned)*
- **Delivery** — Teams, Slack, email *(planned)*
- **Identity** — OIDC providers *(planned)*

Naming Azure DevOps is fine; it is a product thousands of organisations run. Naming *your*
organisation's instance is configuration and does not belong in this repository.

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
