# 0010 — Views: one lens per hub, set by clearance, not by topology

**Date:** 2026-10-29 · **Status:** proposed · **Supersedes:** nothing; builds on
[`0007-hub-network-graph-and-channels.md`](0007-hub-network-graph-and-channels.md) (channels,
per-owner clearance) and
[`0008-control-loop-personal-and-company-hubs.md`](0008-control-loop-personal-and-company-hubs.md)
(partitions, the switchboard, the personal hub holding no company content)

> A decision with no revisit condition is doctrine. Every record here carries one.

**Built:** step 1 of §6 (`GET /views/me`). Everything else here is a decision about shape.

## Context

A deployment has hubs at several levels: a company hub, team spaces or team hubs, and each
person's personal hub, which the harness embeds. People ask each of them the same question,
"what is mine to do, and what is happening around me?", and today every hub answers it
differently or not at all:

- The hub has **no view of its own.** Its read side is the change feed (`GET /events`), the
  queue (`GET /work`), the received digests (0005, `DigestReceived` events) and the memory
  routes. A dashboard is "read the feed" (`agentco/digests.py`).
- **A plain `GET /work` returns the whole queue to any signed actor.** `authz.check_read_agent`
  refuses only a query naming someone else; with no `agent` parameter the list is unfiltered.
  That is the full view, given to everyone by default.
- The obvious next step, one visualization per level (a person view, a team view, a company
  view), would be three readers of three shapes, and each would have to re-implement the wall.

Two designs are tempting and wrong:

1. **The personal hub mirrors the team.** It would subscribe to each team space and render it
   locally. That stores company content on the personal hub, which 0008 §1 rules out: "the
   personal hub holds no company content and is not on the loop", and the memory channel never
   runs between company hubs (0008 §2).
2. **A level above reads the level below.** A team hub that can read a person's hub, or a company
   hub that reads team spaces raw, turns the person's own memory into a monitoring feed. 0007 §2
   already says a personal hub never sends its own items anywhere.

## Decision

### 1. A view is a projection the hub computes for the signer

A view is read-only and computed per request. **Who it is for is the signature, never a query
parameter**, which is the same rule as `authz.read_agent` (0008 §6). There is one projection
shape for every level. A UI, the switchboard, an MCP tool and a CLI all render the same
projection, and no level gets a separate reader.

### 2. Each hub shows two things, and only two

| | Its own items | The level above |
|---|---|---|
| What | items this hub is authoritative for, filtered by the viewer's clearance (0006 §2) and, once 0009's spaces exist, grouped by space | what this hub was **sent** on a declared channel: notices (digests), memory subscriptions (pinned replicas) |
| Freshness | live | every row carries its source hub, version and age; nothing un-labelled is shown as current |
| Writes | through the ordinary routes | none; a replica is changed only at its authoritative hub |

**Nothing is shown from the level below except what that level chose to send up**: a digest
(0005), a promoted item (0009 §4), an answer (0008 §3). Visibility upward is publication.
Visibility downward is subscription.

### 3. The personal hub's view of the team is a lens, not a copy

For the personal hub embedded in the harness, "the level above" is each company the person
works for, and 0008 has already fixed how it is reached:

- The **list** is the switchboard's fixed projection `{label, ref, title, ageSeconds}`, one row
  per open item, from each partition's poll. Titles follow 0008 Decision 5's per-company switch.
- **Drill-in** is a live read by that company's partition from its own hub. It is rendered, then
  dropped. It is never stored on the personal hub.
- **The person's own memory** is the personal hub's own items, shown in full.

So the person view has three panels: *mine* (own items), *asked of me* (vehicles across
partitions, as label, ref, title and age only) and *per company* (live, one tab per partition).
**There is no merged panel of company content.** The only surface where two companies meet is
the switchboard's deterministic list, which 0008 §1 already bounds.

### 4. The same rule at every level

| Viewer's hub | Sees in full | Sees as a labelled replica or live read | Never sees |
|---|---|---|---|
| Company | company space | — | team spaces raw; personal hubs |
| Team (space or hub) | team space | company space, by subscription | sibling teams; personal hubs |
| Personal (harness) | the person's own items | each company's open items for the person, via its partition | other people's hubs; any company's content at rest |

A team that is a **space** in a company hub (0009 §3) and a team that is its own **hub**
(0007 §1's boundary rule) get the same view. The only difference is whether "the level above"
is a sibling space on the same store or a subscription across a connection.

### 5. The wide reads stay, and are named for what they are

The unfiltered `GET /work` is the operator's view, and other readers depend on it today. This
record does not narrow it. Instead:

- `GET /views/me` is the default read for a person, a UI or a partition.
- Narrowing the plain list to the operator, so a non-operator read is the lens, is an open item
  (§7). It changes a wire behaviour, so it needs its own decision and a migration note.

### 6. Order of build

1. **`GET /views/me` on any hub** *(built)*: the signer's open vehicles (*asked of me*) and open
   assigned work (*mine*), each row `{id, ref, title, status, ageSeconds}`, plus the hub's id and
   label. Pure projection over the queue (`agentco/views.py`), with no new store.
2. **Notices panel:** received digests, read from the feed, each labelled with its sender and age.
3. **Memory panel:** items the viewer is cleared for (`walls.effective`), once 0009's spaces
   exist, grouped by space.
4. **The switchboard on the harness**, rendering `{label, ref, title, ageSeconds}` from each
   partition's `GET /views/me` (or the transport's ready read), with drill-in as a live read.
5. **One UI component** parameterised by `(hub, viewer)`. It is pointed at whichever hub you
   are on, and has no per-level fork.

## Alternatives

- **One visualization per level.** Rejected: three readers, three re-implementations of the
  wall, and the personal one would want a store of company titles (0008 alternative (a)).
- **A merged "all my teams" view on the personal hub.** Rejected: it is a cross-company context
  by construction, and the next step is a model reading it.
- **Put the view on the operator's dashboard only.** Rejected: the person, not the operator, is
  the one who has to act on what is asked of them.

## What is NOT claimed

- That the projection is a UI. Step 1 is a JSON read; how it looks is step 5.
- That `GET /views/me` hides anything the signer could not already read. It is a narrower
  default, not a new wall. The wall is still clearance and the transport class.
- Anything about credit or ranking inside a view. Rows are ordered by age, oldest first.

## Costs and consequences

- **Two reads now give the same rows** (`GET /work?ready=1` and part of `GET /views/me`). The
  view is a projection over the same queue, so the two cannot disagree, but a client has to
  pick one.
- **Live drill-in needs the partition to be reachable.** When a company hub is down, its tab
  shows "unreachable" with the age of the last list. It never shows a cached body.
- **A view that is wrong is quietly wrong.** Every non-authoritative row carries its source and
  age, so staleness is visible rather than inferred.

## Open items

- Narrow the plain `GET /work` to the operator (§5), with a migration note.
- Decide whether the transport class reaches `view_me`. Today it does not: its ready read
  already gives the partition what it needs, and widening a transport credential is the
  direction 0008 §3.3 is built to resist.
- Notices and memory panels (§6 steps 2 and 3).

## Revisit condition

Revisit if a level needs to **act on** another level's content at rest (not a live read, not a
labelled replica), because that would mean the two belong in one hub, or if people routinely
open the per-company tabs side by side to compare them, because that is a merged view asked for
in practice and the switchboard boundary would need to be re-argued.
