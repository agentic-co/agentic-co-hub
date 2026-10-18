# 0007 — The hub network is a graph, and every connection declares its channels

**Date:** 2026-10-16 · **Status:** accepted (owner, 2026-10-16); implementation
planned · **Supersedes:** nothing; extends
[`0005-federated-hub-topology.md`](0005-federated-hub-topology.md) and replaces
two of its limits (push-only, one upstream) — see "What 0005 already says, and
what this adds"

> A decision with no revisit condition is doctrine. Every record here carries one.

Nothing in this record is built. 0005's push-only digest remains the only
hub-to-hub path that exists.

> **Status of the open points (owner, decision 8, 2026-10-18):** the identity
> mechanism (§4) and credit across hubs (§5) are **open**, each with its reason and
> revisit condition below.

## Context

0005 made federation possible by recursion: a parent hub is just another actor
on a child hub's registry, and the child files a cadence digest upward. It was
deliberately narrow, and its own correction says so: nothing flows back down,
and a parent pushing work to its children "is new scope, not something this ADR
already covers."

Two things a tree with one upstream cannot express are now concrete:

1. **One person belongs to several hubs at once.** Picture a consultancy: a
   company hub, one hub per client, hubs per team, and each person's own
   personal hub. That personal hub connects to the company hub *and* to two
   client hubs. The structure is a graph, not a tree, and the current
   configuration — one parent URL per hub — cannot describe it.
2. **Different connections must carry different things.** The client hub
   should send work down to the person's hub and receive results back, and
   nothing else. The company hub should receive a digest and publish
   procedures. A personal hub should never send its own items anywhere. 0005
   has one kind of connection carrying one kind of payload, and the digest it
   carries passes the child's metadata upward **unfiltered**.

The question this record answers is what a connection between two hubs is, and
how work finds its way across the resulting graph without a central scheduler.

## Decision

### 1. A graph of hubs and harnesses

A hub may hold **any number of connections**, to parents, peers or children.
Each connection is declared on both ends, is directional for each thing it
carries, and is independent of every other connection the hub holds.
Disconnecting one costs the hub nothing locally, which is 0005's property
carried over unchanged.

**A separate hub is warranted only by a real boundary**: a different data owner,
a different person, or a different operator or uptime requirement. Departments
inside one organization with one data owner are teams within a hub, not
separate hubs. This is the answer to "why not one hub per team?" — a hub is a
boundary, and a boundary that is not real is overhead.

### 2. Every connection declares its channels

A connection opens a **subset** of four channels. Anything not opened does not
flow.

| Channel | What it carries | Direction |
|---|---|---|
| **work** | tasks to be done | from the hub that owns the work toward the hub that executes it |
| **answers** | results of that work, and answers to questions it raised | back toward the hub that sent the work — results only, so a personal hub stays personal |
| **memory** | ASOPs and shared lessons by pinned-version subscription; outcomes and proposed changes in return | subscriptions toward the replica, outcomes and proposals toward the item's authoritative hub (0006, the memory record proposed alongside this one) |
| **notices** | digests, alerts, status | upward by default; 0005's digest is a notice |

Each connection also carries a **clearance per data owner** — an `(owner,
level)` pair, compared only within the same owner. Clearance is a signed
credential that **only narrows per hop**: a hub can pass on less than it was
granted, never more. It is a hard gate applied before any routing or ranking,
never a weight. Everything a channel carries, including a notice, passes the
connection's clearance filter; that closes the unfiltered-digest gap above.

### 3. The hub is a board, and runtimes self-select

Coordination across the graph is **stigmergic**: actors coordinate through
marks left on a shared medium rather than through a central planner. The hub is
that medium. It holds pointers, leases, channel policies, approval queues,
baselines and audit samples — not work content.

- **Every claim is a lease.** Cancelling is "stop renewing"; staleness is the
  default, not a failure. This is the lease the work queue already uses,
  applied across hubs.
- **Work emits a stimulus** that grows with age and priority. An eligible actor
  — one whose clearance and capabilities match — pulls it when the stimulus
  passes its own threshold. Nobody assigns it.
- **The protocol has explicit verbs**, including **Decline** and return with a
  reason. A runtime that cannot do the work says so; it does not sit on it.
- **Every completion carries an evidence pointer.** Renewing a lease means
  progress, and progress means re-checking the pointer at its source.
- **Unclaimed work escalates to a human**, through the review routing the
  architecture already plans. **WIP limits** give backpressure.
- **No internal market.** Agents bidding for work is cheap talk: a bid costs
  the bidder nothing and proves nothing.
- **Audits and alerts follow the viable system model.** A random audit
  re-opens a completed pointer to check its evidence. An alert that matters
  (an algedonic signal) skips levels and goes straight to the owner rather than
  climbing the graph one hub at a time.

On the harness side this means one partition and one session per data owner,
pulling work when the stimulus passes its threshold, and declining with a
reason when it should not take the work.

### 4. Identity is federated through an external identity provider

Every action across the graph has to be traceable to a **(human, harness)**
pair, so that a defect can be fixed where it originated. The direction is:

- A human authenticates once with their organization's identity provider —
  Microsoft Entra ID is the first example; any OIDC provider is the generic
  case.
- That authenticated session is what may **register a harness**. Registration
  yields a token that binds the harness to the human who registered it.
- Hubs trust the identity provider, not each other's word, for who a human is.

The direction is accepted. The mechanism is not: established patterns for
multi-agent and delegated authentication have to be reviewed before choosing
one, and OIDC is a candidate, not a settled choice. This is the roadmap's
existing "identity beyond HMAC" item, given a reason and a shape.

**Status: open** (owner, 2026-10-18). The direction — a (human, harness) pair bound
through the organization's identity provider — is accepted; the mechanism is not
chosen, at **confidence 0.55**, because the review of delegated-authentication
patterns this section requires has not been done, and per-hub HMAC keys remain
what exists. *Revisit when* a second organisation needs to connect a hub.

### 5. Credit across hubs is not decided here

When work crosses several hubs, how credit for the outcome is shared among them
is an open question. The only decision taken is negative: **no internal
market**. Any scheme for attributing an outcome across hubs is new scope with
its own record.

**Status: open** (owner, 2026-10-18). Not decided, beyond the negative: no internal
market. Nothing yet exchanges work between hubs with different owners, so there is
no outcome to attribute and no evidence to choose a scheme from. *Revisit when* two
hubs exchange work with different owners.

## What 0005 already says, and what this adds

| | 0005 | 0007 |
|---|---|---|
| Federation is optional, per hub | yes | unchanged |
| A connected hub is just another actor (recursion, no new protocol) | yes | unchanged; channels are declared on that same connection |
| The far side knows the hub, not its agents | yes | unchanged |
| Rollup, not replication | yes | unchanged for notices; work and memory are new channels, not replication |
| Disconnecting is free and lossless | yes | unchanged, per connection |
| Fail-closed allowlist of accepted children | yes | generalized: no channel opens without an explicit declaration on both ends |
| Direction | **push-only**; nothing flows down | per channel; work flows down, answers and notices up, memory both ways by role |
| Connections per hub | one upstream | any number |
| What a digest may carry | child metadata, unfiltered | filtered by the connection's per-owner clearance |
| Identity | per-hub HMAC keys | plus a human-to-harness binding through an external identity provider |

0005's revisit condition — a live, not digest-cadence, view at the parent — is
not what triggered this record. That condition still stands on its own.

## Costs and consequences

- **More configuration per connection.** Channels and per-owner clearance are
  declared on both ends. A connection that declares nothing carries nothing,
  which is safe and also the likeliest first-day confusion.
- **Self-selection needs honest thresholds.** If every runtime sets a low
  threshold, the board degenerates into a race; WIP limits and lease expiry are
  what keep it from becoming one.
- **Escalation becomes load-bearing.** Work nobody chooses lands on a human.
  Until review routing is built, that is the human-gate machinery the plane has
  today, which names a human but has no claim deadline or defer reason.
- **The identity provider becomes a dependency** for registering harnesses. A
  hub with no provider configured keeps today's per-hub keys.
- **A graph can cycle.** Work forwarded from hub to hub needs a hop limit or a
  visited set; the narrowing-only clearance bounds what leaks, not how far work
  travels.

## Open items for the implementation

- Multiple connections per hub, replacing the single parent URL.
- The channel declaration and its enforcement on both ends.
- Signed, per-hop-narrowing clearance credentials, applied before routing.
- Clearance filtering on the existing digest path.
- Work stimulus, thresholds, the Decline verb, evidence pointers, WIP limits.
- Random audit of completed pointers; the algedonic path to the owner.
- Cycle protection for forwarded work.
- A review of established delegated-authentication patterns, then the
  identity-provider binding of harnesses to humans.
- Credit assignment across hubs, if and when it is needed, as its own record.

## Revisit condition

Revisit if a deployment needs a separate hub that none of the three boundaries
(data owner, person, operator) justifies — that would mean the boundary rule is
wrong — or if self-selection leaves work unclaimed often enough that the human
escalation queue, rather than the board, is doing the routing.
