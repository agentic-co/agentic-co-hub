# 0006 — Shared memory in the hub: owner-labeled layers, judged by use

**Date:** 2026-10-16 · **Status:** accepted (owner, 2026-10-16); implementation
planned · **Supersedes:** nothing; extends [`docs/decisions/asop.md`](asop.md)
(proven lessons end up inside ASOPs) and relies on 0007 (the hub network
record, proposed alongside this one) for the memory channel between hubs

> A decision with no revisit condition is doctrine. Every record here carries one.

Nothing in this record is built. It fixes the shape the implementation has to
take, and it says which parts of that shape are still guesses.

## Context

### What shared memory is for

The README says the plane sits *above* agent memory: an agent's own recall is its
harness's business. That stays true. What is not any one harness's business is the
lesson one agent learned that another agent, in a different runtime, owned by a
different person, should not have to learn again. Today that lesson either stays
locked in the runtime that learned it, or it escapes through whatever copy of
notes, cloned repositories and synthesized summaries the people involved happen to
keep — and that second path is where context from one data owner ends up in
another owner's work.

The hub already holds one kind of shared memory: the ASOP. A procedure is
procedural memory with a version, a gate and an outcome record. This decision
treats lessons and procedures as **one memory system**, so a lesson can be
captured, proven, shared under a policy and eventually written into the
procedure it belongs to.

### The failure modes the first draft had

A first design draft was reviewed against established models of memory and of
organizations. It had six defects worth naming, because each one is a mistake
the implementation could easily make again:

1. **Superstitious credit.** The draft reinforced every item retrieved into a
   session that ended well. Most retrieved items were never used, so the rule
   rewarded retrieval itself and would teach agents to pull everything in.
2. **Decay without an exempt class.** Every item faded on disuse. Constraints
   and rare-but-critical facts are exactly the items that are seldom used and
   must never fade.
3. **One agent paper's constant taken as biology.** The draft's 29-day
   half-life came from a single paper on LLM-agent memory, not from any study
   of natural memory. Most numeric decay and reinforcement parameters in the
   draft had the same single origin. They are engineering choices and must be
   labeled as such.
4. **A wall that covers content only.** Owner isolation filtered what an item
   *says*. It did nothing about metadata, timing, or the bare fact that an item
   exists — each of which can leak.
5. **Per-item residuals checks blind to set-level leaks.** A checker that
   judges each outgoing lesson alone cannot see that ten individually clean
   lessons, read together, reconstruct one owner's specifics.
6. **White-box audits applied to text.** Membership-inference and similar
   privacy audits probe model weights. The draft proposed them for text notes,
   where they do not apply in that form.

A seventh finding shaped the usefulness rule rather than correcting a stated
defect: in biology the consolidating event need not be related to the event it
consolidates, and aversive outcomes consolidate as well as rewards. A rule that
credits only *same-session verified success* is stricter than the model it was
borrowed from, and it cannot learn from failures.

### Terms used once and precisely here

Three terms mean different things in the different bodies of work the review
drew on. In this record they mean exactly this:

- **Label creep** — security labels ratcheting upward when items are combined,
  until everything sits at the most restricted level and nobody can read it.
  An over-restriction failure. Its opposite is **provenance loss**: content
  kept, origin lost, which is a leak. The two are measured separately.
- **Autoimmune** — the wall refusing an owner's own legitimate items, or a
  validator refusing clean residuals. Over-rejection, not a leak.
- **Reconsolidation** — an item being re-checked and possibly revised at the
  moment it is revived or used, rather than staying frozen as first written.

## Decision

### 1. Owner-labeled layers, with one authoritative hub per item

Every memory item carries an **owner label**, a pair `(owner, level)` such as
`(acme, internal)`, `(personal, private)` or `(public, all)`. Levels compare
only within the same owner; `(acme, internal)` and `(globex, internal)` are not
the same clearance.

Memory sits in layers, from most volatile to most shared:

| Layer | What it holds | Leaves its hub? |
|---|---|---|
| session | working context for one run | no |
| episodic | transcripts and raw traces | **never** |
| personal | one person's items, partitioned by owner | only through the residuals path (§3) |
| shared | a team, organization or client hub's items | over a memory channel, by label |
| public library | items labeled `(public, …)` | yes |

**Each item has exactly one authoritative hub.** Other hubs hold versioned
replicas. Subscriptions flow toward replicas; proposed changes flow toward the
authoritative hub, which accepts or rejects them — the same shape as a pull
request. A replica never becomes a competing version of the fact, which is the
plane's existing law (`docs/architecture.md`, Boundaries) applied to memory.

What the hub holds is the shared layer and above: short authored records, the
same class of object as an ASOP it already stores. Session and episodic memory
stay in the harness. Source material stays where it lives, referenced by
pointer, never copied.

### 2. The wall, and clearance per data owner at hub and agent level

Every hub connection and every registering agent declares a clearance **per
owner**. Two clearances apply and both must say yes:

- **Hub clearance** limits what the hub's own router and retrieval may see.
- **Agent clearance** limits what an agent may open, enforced by the source
  system's own credential, not by the hub's word.

Retrieval enforces labels with **deterministic code, before any ranking**.
Clearance is a gate, never a weight; a language model is not asked to respect a
label, because it cannot be relied on to.

The real leak path is the agent: one cleared for two owners can carry the
first owner's content into the second's work through its own memory. Clearing
an agent for an owner therefore also means **one partition and one session per
owner** in that agent's harness.

### 3. Residuals authority (D1)

Isolation is strict by default. General know-how may leave an owner's partition
only by a sanctioned path, shaped as a **clean room**, not as the residuals
clause of a non-disclosure agreement (which lets a person keep what they
happen to remember; a clean room controls what is written down and who judges
it):

- The **owner defines the categories** of lesson that may leave, plus a
  **never-leave list** that no approval overrides.
- An agent proposes a residual: a newly written item with no names, identifiers,
  URLs, code or data, carrying a provenance pointer that only an owner-cleared
  session can open. Provenance is a hard field, not a comment.
- A **blind validator** with zero context about the source judges whether the
  residual stands on its own. Code checks the mechanical part (owner terms,
  identifiers, hostnames, repository names, people); the model writes, code
  judges.
- An **aggregate check per owner** looks at everything that has left that
  owner's partition together, which is what a per-item check cannot see.
- The owner **approves in batches**, not item by item.
- A category with a long, clean approval record may **graduate** to automatic
  release with notification, and graduation is **reversible**: one bad release
  sends the category back to manual approval.

The same path applies to ASOPs.

### 4. Usefulness is judged by use (D2)

Usefulness cannot be known at write time, so it is judged by later use.

- **Admission by surprise.** Capture admits an item only if it is new relative
  to what memory already holds and is actionable. Everything else stays
  episodic.
- **Credit only what was used.** An item is credited only if it was **invoked
  in the action trace** — it demonstrably shaped an action — not merely
  retrieved into the session.
- **Three-valued, externally verified outcome.** The outcome an item is
  credited against is verified outside the agent (tests, a merged change, a
  closed-and-not-reopened work item) and takes one of three values:
  verified success, verified failure, unverified. A lesson invoked in a
  verified failure is evidence too; unverified outcomes credit nothing.
- **The owner's rating is the strong event.** An explicit rating by the owner
  outweighs any number of automatic credits.
- **The clock is exposure, not the calendar.** An item ages when it was
  *available and not used*, so a quiet month does not erode a lesson nobody had
  occasion to need.
- **Retention by use.** Items that keep being used stay warm.
- **Decay never deletes.** A cold item moves to cold storage that stays
  auditable but is not loaded into context. It revives either by **demand
  pull** (a retrieval asks for it) or by **periodic replay** (a scheduled pass
  re-tests cold items). A revived item returns **on probation**, and is
  reconsolidated on return. Disuse is not evidence of uselessness.

### 5. Decay form (D3)

- **Power-law decay with an activation floor**, on the exposure clock of §4.
  The floor keeps an item retrievable at low weight rather than letting it
  reach zero.
- **A pinned class** exempt from both decay and reinforcement: constraints,
  safety rules, rare-critical facts. Pinned items are reviewed on the
  **calendar**, by a person, because use is the wrong signal for them.

### 6. Proven lessons live inside ASOPs

The long-term home of a proven lesson is the procedure it belongs to. An
organization forgets what it learned when the lesson lives in a person or in a
side store rather than in the routine that runs every time; a lesson written
into an ASOP version is applied on every run and is measured by that version's
outcomes. So the end of the pipeline is a **proposed ASOP revision**, through
the improvement loop that already exists (adjudication → `plan_vs_actual` →
`agentco lessons --propose`). A lesson that has been folded into an ASOP stops
competing for context as a separate item.

## What is NOT claimed

- **No parameter is validated.** The decay exponent, the floor, the probation
  length, the graduation threshold and the replay interval are engineering
  choices to be measured, each labeled by origin when it is set. The *structure*
  of the wall (two layers of tolerance) is adopted from the models; no number
  is.
- **Metadata, timing and existence leaks stay open.** The wall in §2 covers
  content. That an item exists, when it was written, and how its replicas
  change can still reveal something. This record does not close those.
- **No privacy audit is claimed for text memory.** White-box audits are out of
  scope until a method that actually applies to text items is chosen.
- **Biology is a design bias, not a guarantee.** Where the models are soft,
  the guarantee comes from deterministic pruning before retrieval.

## Evidence

The shape above was graded, not just argued. A research pass drew on three
bases (memory biology; cybernetics, organizational learning, immunology and law;
and the project's own implementation) and graded **36 hypotheses**: **24
supported, 12 partially supported, none contradicted.**

The models the parts map to: the layers follow complementary-learning-systems
and multiple-trace accounts of memory, with organizational knowledge
depreciation as the same shape at company scale (which is what forces §6); the
wall follows two-layer immune tolerance; the residuals path follows clean-room
practice.

**These verdicts are answer-level.** They grade answers synthesized from
source collections, not the primary sources themselves. A primary-source
spot-check is pending, and until it completes this record deliberately cites
no paper. If the spot-check moves a verdict, the affected section is revisited
here, in the open.

## Costs and consequences

- **Every item gets heavier.** Owner label, provenance pointer, exposure
  counter, outcome credits and a pinned flag on every item. Cheap per item,
  real in aggregate, and every one of them is a field a migration has to carry.
- **Credit needs a trace.** "Invoked in the action trace" requires harnesses to
  report which items they used. A harness that does not report earns its items
  no credit. That is the honest default and also a participation cost.
- **Verification is borrowed.** The outcome signal is only as good as the
  external checks behind it. Work with no verifiable outcome teaches memory
  nothing, by design.
- **The owner's queue is the real limit.** Batch approval and category
  graduation exist to keep it short; if it still grows, residuals will be
  approved without being read, which is the failure the batch was meant to
  prevent.
- **Cold storage grows forever.** Never deleting is a choice to pay storage and
  replay cost for the chance that an old item becomes relevant again.

## Open items for the implementation

- The item record and its storage, alongside the ASOP store.
- Label enforcement in retrieval, before ranking, with tests that fail if a
  label is bypassed.
- Per-owner partition and session isolation in the harness, as a connector
  requirement.
- The residuals pipeline: category registry, never-leave list, mechanical
  checker, blind validator, aggregate check, batch approval, graduation and
  reversal.
- Action-trace reporting from harnesses, and the three-valued outcome binding.
- Exposure clock, power-law decay with floor, pinned class with calendar review.
- Cold storage, demand-pull revival, periodic replay, probation.
- The lesson → ASOP revision hand-off through the existing improvement loop.
- Separate measurements for label creep and for provenance loss.
- Choosing every parameter, with its origin recorded.
- The primary-source spot-check of the 36 graded hypotheses.
- The review also logged further evidence-forced changes as scope for this
  record without separate decisions. Those it states are folded into the
  sections above; any it does not state concretely are for the implementation
  to raise as their own change, not to infer from here.

## Revisit condition

Revisit if, once built, the owner's approval queue stops being read (approvals
arrive faster than a person could have read them), if a measured leak passes
the aggregate check, or if the spot-check moves any verdict this record relies
on from supported to contradicted.
