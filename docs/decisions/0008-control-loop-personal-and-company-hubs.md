# 0008 — The owner's control loop: pull first, one partition per company, answers are the owner's attestation

**Date:** 2026-10-18 · **Status:** proposed · **Supersedes:** nothing; narrows
[`0007-hub-network-graph-and-channels.md`](0007-hub-network-graph-and-channels.md)
for one topology and amends its direction table for the case where the hub that
owns the work also executes it (§2)

> A decision with no revisit condition is doctrine. Every record here carries one.

Nothing in this record is built. 0005's push-only digest is still the only
hub-to-hub path that exists.

## Context

One person works for several organisations at once — a consultant with three or
four client companies. 0007's boundary rule gives each company its own hub,
because each is a different data owner. Each company hub files that company's
work from its own tracker and meetings, and a runner on that company's machine
executes it. The person also has a personal hub and a personal runner on their
own machine.

What the person needs is one loop: questions and parked gates from every company
reach them in one place, and their answers and instructions go back to the right
company, without switching machines — and without any company's content meeting
another's.

Three facts about the code shape the answer:

1. **The question record already exists.** For every parked or failed human gate,
   the pulse files a *vehicle*: a work item titled `Verify: <title>`, assigned to
   the gate's named verifier, keyed `verify:<item>:<attempt>`, retired when the
   attempt moves (`agentco/verifiers.py`). Its own docstring names the missing
   piece: the vehicle exists and nothing tells the person.
2. **A human gate is answered only by the named person's own signature**
   (`Queue.attest`). That is ASOP §5.3 ("naming is not authenticating") and §7.3
   (a plane may route a gate it does not own; it may not answer it).
3. **Any key on a hub is full access today.** `GET /work` returns every item in
   full, and any key may file and claim work. And `GET /events` on a parent shows
   every child's digest to every child. A credential that is meant to be narrow is
   not narrow yet.

## Alternatives

**(a) Push first.** Each company hub sends its open questions up to the personal
hub as notices; the personal hub renders them. This lets the company decide what
leaves. But while every key is full access (fact 3) it buys nothing the person's
own key does not already have, it needs new hub code before anything works, and
it puts a store of company titles on the personal hub — the place this design
must keep free of company content.

**(b) A relay answers on the person's behalf.** The personal hub, or an actor
declared for it, signs answers with `on_behalf_of`. Rejected: it is a plane
answering a gate it does not own (§7.3), and its signature is not the person's
(§5.3). Nothing in the spec permits a delegate to answer a human gate.

**(c) Pull first.** Each company partition on the personal runner polls its own
company hub with the person's own per-company credential, keeps only the
vehicles assigned to them, and hands a deterministic switchboard a fixed
projection. Answers are signed by the person's per-company key and land on the
owning company hub. No hub code is needed for the first loop, and the personal
hub holds nothing from any company.

## Decision

**(c), with push as a later step under stated conditions.**

### 1. One partition per company; a switchboard with no model

The personal runner keeps **one partition per company**: that company's
credential, sessions and any model context live there and nowhere else (0006 §2,
0007 §3). A partition polls its company hub for ready work assigned to the person
and keeps only vehicles. It hands the switchboard exactly
`{label, ref, title, ageSeconds}`: `label` from the partition's configuration,
never from hub data; `ref` the vehicle key; `title` the vehicle's subject title;
age from when it was routed.

The **switchboard is deterministic and holds no model credential and no company
credential.** It renders rows as text and, on selection, passes `ref` to the
partition named by `label`. This is the mechanical form of "never one model
context holding several companies' content", and it is testable: the
switchboard's process has no key.

Drill-in is a **live read by that partition from its own hub**. Nothing is copied
to the personal hub or the switchboard.

### 2. "Parent" is per channel

0007 says work flows from the hub that owns it toward the hub that executes it,
and answers flow back toward the sender. Here the company hub owns **and**
executes, so the inversion applies to both channels:

| Channel | From | To | Authoritative |
|---|---|---|---|
| notices | company hub | the person's view (personal hub once push exists) | company hub |
| answers | the person, through that company's partition | company hub | company hub (the owning store, §7.3) |
| work | the person, through that company's partition | company hub | company hub, once filed |
| memory | — | — | never between company hubs |

The personal hub is the **parent on the notices channel only**. It never owns a
company's work and never answers a company's gate. This amends 0007's direction
table for the owner-is-executor case.

### 3. Answers are the person's own attestation

An answer is `POST /work/{id}/attest` on the company hub, signed with the person's
own key for that company, held only in that company's partition. The company hub
re-checks bead, gate, registry and status before it flips anything (§7.3). The
first valid answer wins; a second is refused. A tracker comment is not an answer:
nothing can authenticate it as the person.

### 4. Instructions are ordinary work

An instruction is `POST /work` on the company hub with a natural key from the
person's partition, so a retry files once. The company's runner pulls it like any
other item.

### 5. Push comes after credentials are narrow

Push notices are built only after (i) edge credentials are limited to a read
shape and projection and a child's key on a parent reaches only its own rows, and
(ii) answers can be carried by a channel-scoped transport key (§Open 1). Then:
rows are **derived from vehicles**, sent as a **state snapshot** upserted latest
per child, strict on the receiver, to an **explicit target** — a company hub may
also have its own parent.

### 6. Conformance fixes this loop depends on

These are defects against the spec as it stands, not new design:

- **Epoch on every attestation.** §7.2 requires every flip, completion and
  attestation to carry the ownership epoch. The hub checks it on renew and report,
  not on attest.
- **The second answer's refusal code** is `not_terminal` (§7.3); the hub answers
  `attestation_invalid`.
- **Signed queries on.** `ASOP_REQUIRE_SIGNED_QUERY=1` on every hub in this
  topology, so a captured signature cannot be replayed as a different query.

## Open

1. **Signed envelope.** The target answer model: the person's per-company key
   signs `(item, attempt, epoch, verdict digest, nonce)`, and a channel-scoped
   transport key carries it; the hub verifies both and records the person as
   `submitted_by` and the transport actor as `via`. With the signing key behind an
   operating-system presence check, "a human confirmed this" becomes something
   the hub can verify. It needs no identity provider, so it does not wait for
   0007 §4. Mechanism and wire shape are not chosen.
2. **An attempt fence in the spec.** Proposed for asop-spec: an attestation
   carries `attempt`, and one whose attempt is not the item's current one is
   refused `stale`. Until then a stale answer can land, and the partition's
   re-read before sending is the only guard.
3. **Gates stranded by revocation.** A gate stores its verifier by name. Removing
   the person from a company hub leaves every gate naming them unanswerable; no
   verb reassigns a verifier. Until decided, the operating rule is to rotate the
   person's key rather than remove the identity. A recorded administrative
   reassignment, parallel to run transfer (§7.2), is the candidate.
4. **Titles.** A tracker title can name a customer or an incident. The
   deterministic switchboard keeps it away from any model holding other companies'
   rows, not away from the screen. Whether a company may replace titles with its
   own short label is open.
5. **Identity across organisations** stays open as 0007 §4 left it.

## Consequences

- **The first loop needs no hub code.** A test rig with a personal hub, three
  company hubs and a reference pull client can show question up, answer down,
  using only existing routes.
- **The person's key is full access** on every company hub in the first version.
  That is no more than the person already holds, and no less: a stolen personal
  machine is a stolen consultant until the envelope and narrow credentials exist.
- **The partition rule is a runner property.** The hub cannot see a model
  context. What it can enforce — who signed, which gate, which attempt and epoch,
  which fields cross — it should; the rest is stated as the runner's duty.
- **Latency is the pulse cadence plus the poll interval.** Vehicles are filed by
  the pulse, not at the moment an item parks.
- **Timing and existence still leak.** Row counts and ages per label reveal
  activity per company to whoever sees the switchboard (0006, open).
- **The personal hub stays personal.** It holds no company content, which keeps
  0006's wall trivially true on it.

## Revisit condition

Revisit if a company needs to control what leaves its hub before the person's
credential is narrowed (that would pull push forward); if the switchboard needs a
model to be usable (that would break the partition rule as stated here and need a
different enforcement); or if the spec comes to permit a delegate to answer a
human gate (that would reopen alternative (b)).
