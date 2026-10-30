# 0009 — Ontological memory: entity facts, spaces, promotion, and the memory steward

**Date:** 2026-10-29 · **Status:** proposed · **Supersedes:** nothing; extends
[`0006-hub-memory-design.md`](0006-hub-memory-design.md) (memory items, the wall, credit by use) and
narrows how [`0007-hub-network-graph-and-channels.md`](0007-hub-network-graph-and-channels.md)'s
memory channel is first used

> A decision with no revisit condition is doctrine. Every record here carries one.

Only spaces (§3) are built; see Order of build. The record names the shape, the rules the shape must keep, and the
measurements that would show it is wrong before much of it is built.

## Context

### Two kinds of memory are being mixed in one field

The hub holds **procedural memory**: an ASOP version says how a kind of work is done, and its
`common_mistakes` (capped at five by the contract) carry the lessons that version learned. ADR 0006
added short authored items (lessons, constraints, facts, notes) beside it.

A third kind of content keeps landing in `common_mistakes` because there is nowhere else to put it:
**facts about one particular thing**. "This repository runs its suite with `python3 -m pytest`."
"This service deploys from the `release` branch." Such a fact is not procedure. It makes a general
procedure fit one repository, it spends one of five lesson slots, and when it goes stale a run fails
and the failure is credited against the procedure version, which did not change.

A runtime already meets this gap from the other side. Its lesson-triage procedure classifies every
lesson as `fact`, `defect` or `noise`, and requires a `fact` to name where it was recorded. There is
no typed place to record it.

This record calls the second kind **ontological memory**: typed entities and the facts held about
them. Procedural memory answers *how this work is done*; ontological memory answers *what is true
about this thing*. Every run applies one to the other: one procedure version against one set of
entity facts.

### Why the two have to stay independent

If both axes are pinned separately, a failed run can be attributed to the one that broke: the
procedure (a lesson for the next ASOP version) or a fact (a correction to the entity). Today both
land in the same list and the system cannot learn which. Keeping the axes orthogonal is the property
this record exists to protect, and it is also the property the validation section tries to falsify.

### An external proposal, and what it got wrong for this plane

An outside design for "a hub ontology" proposed a zero-touch pipeline: ingest commits, documents and
runtime traces; extract graph mutations with a model; validate them against invariants; and recompile
procedures into workflow-engine definitions, without a person in the loop. Its central idea, that
environmental facts belong on typed entities and not in procedure steps, is the idea this record
adopts. Most of the machinery around it collides with rules this plane already depends on:

| Proposed | Rule it meets | Consequence here |
|---|---|---|
| The hub compiles procedures into a workflow engine and reloads workers | The hub executes no work; gates evaluate in the runtime | A workflow engine may be a runtime backend, never the plane's job |
| Procedures recompiled with no human | The revision policy: agent revisions are drafts; a human activates | Facts may change without a person; procedures may not |
| Facts resolved live while a run is in flight | An instance reads the version it was pinned to (`sop_plan`), and outcomes are grouped by version | Facts are resolved **when the run is filed** and frozen with it |
| Facts templated into gate commands | A deterministic gate's `check` runs through a shell in the runtime | A model-extracted string must never reach a check: protected facts (§5) |
| A model's `confidence` decides merges | Usefulness is judged by use against a verified outcome (0006 §4) | Self-reported confidence is not evidence |
| One global graph snapshot | One authoritative hub per item (0006 §1) | Authority and versions stay per item |
| Entities with no owner | Every item carries `(owner, level)`; nothing crosses company hubs (0008 §2) | Every entity and fact is owner-labelled |

## Alternatives

**(a) Keep facts in `common_mistakes`.** No new concept. Keeps the attribution problem above and the
five-slot squeeze, and ties general procedures to single repositories.

**(b) A graph database beside the hub, fed by a model.** Expressive, and the shape most proposals
reach for. Doubles the store surface the conformance suite must hold to one semantic core, puts a
model on the write path of the store of record, and introduces a global snapshot that does not
federate.

**(c) Entity facts as a slice of 0006 memory.** A memory item gains a typed subject. Everything 0006
already gives an item (owner label, authority, versions, the wall, credit, decay, audit) applies
unchanged. Entity types are a small schema registry, not a graph engine.

## Decision

**(c)**, with the following parts.

### 1. Entities and facts

- An **entity type** is declared in a registry the operator controls: a name, its properties with
  JSON-Schema types, and the **space that is authoritative for it** (§3). Examples:
  `repository`, `service`, `environment`, `policy`, `customer`.
- An **entity id** is `type:owner/name`, e.g. `repository:acme/payments`. The owner part is the data
  owner of 0006; an entity belongs to exactly one.
- A **fact** is a 0006 memory item with `kind: fact` and two new fields: `subject` (an entity id)
  and `property` (a property its type declares). Its value is validated against the declared type at
  the write boundary; a malformed fact is refused, never stored and ignored.
- A fact keeps every 0006 field: label, layer, authority, version, provenance (required, a pointer),
  pinned, status. Nothing is deleted; a superseded value is a new version.
- A `constraint` item may also take a `subject` (or an entity type) and states a rule facts must
  keep. Constraints are pinned (0006 §5): exempt from decay, reviewed on the calendar.

### 2. Facts are resolved when a run is filed

A procedure step may declare the entity facts it reads, e.g. `facts: {test_runner:
$subject.test_runner}` beside its inputs. At `instantiate` the hub resolves each against the caller's
effective clearance and **copies the resolved values, with each fact's id and version, into
`sop_plan`**. The run reads those values for its whole life; `plan_vs_actual` records them beside the
outcome.

A fact that is missing or not cleared refuses the filing (`fact_unresolved`), naming the subject and
property, the same way `inputs_missing` does today.

This binding is **hub-local** in its first version and conformance-neutral: an ASOP that declares no
`facts` is unaffected. It is to be proposed to asop-spec as an additive change, the path 0008 set for
the answer envelope.

### 3. Spaces: separation inside one hub

ADR 0007 §1 puts a separate hub only where there is a real boundary: a different data owner, a
different person, or a different operator or uptime requirement. Teams inside one data owner are
therefore **spaces within a hub**, and this record makes that a first-class axis.

- Every item gains a **space**: a path such as `agency/team-delivery`, under the hub's root space.
- **Space is a soft wall; owner is a hard one.** An actor's clearance (0006 §2) is still the gate.
  Within what it is cleared for, an actor sees its own space and every ancestor space, merged.
  Spaces separate for relevance and focus, not secrecy. Anything that must be secret from another
  team belongs to a different data owner, and in practice to a different hub.
- The owner label stays **fixed for life** (0006 §1). The space may **widen** (promotion, §4) and
  narrow back, each as an audited version.
- The name is deliberately not "scope": a scope in this plane is a claim's `(repo, path-prefix)`
  area (ADR 0001), and one word meaning two things has already cost this project one rename.

### 4. Promotion: what moves up, and how

Promotion is a change of space within one hub, or, across hubs, a new item over 0007's memory
channel. It is never two copies trying to agree.

An item becomes a **promotion candidate** for the parent space by one of three signals, from cheapest
to strongest:

1. **Type**: its subject's entity type is authoritative in the parent space (a team fact about a
   company `service` belongs to the company).
2. **Demand**: actors outside the item's space keep failing to resolve it, or the parent space's own
   procedures ask for it.
3. **Convergence**: two or more spaces hold equivalent items they wrote independently. Inside one
   hub this is a local query; it is the strongest evidence that a lesson is general.

Candidates are approved **in batches** by a declared human for the parent space, the shape 0006 §3
uses for residuals. A category with a long clean record may graduate to automatic promotion with
notice; one bad promotion sends it back to manual. Within one owner, promotion widens the item's
space and keeps its id, provenance and history. A lesson promoted from several spaces into the
parent becomes a proposal on the **parent's** procedure, through the existing improvement loop.

Downward, nothing is promoted: parent-space items are already visible to child spaces, and a
parent-space `constraint` that a child-space fact violates is flagged (`constraint_conflict`) at the
fact's write and in the pulse.

Across data owners nothing is promoted by this path. That still goes through the residuals clean
room of 0006 §3.

### 5. Two write tiers, and protected facts

- **Deterministic tier.** A parser over structured sources (a package manifest, a build file, a
  structured record in a system of record) writes facts directly, with provenance pointing at the
  source and its revision.
- **Proposed tier.** A fact extracted by a model from unstructured material (a design record, a
  change description, a log) enters as a **proposal**, not a fact. It must carry a provenance pointer
  to the span it came from. It is approved in batches, or graduated by category as in §4.
- **Protected facts.** A property a gate `check`, a command, a `money` step or an `irreversible`
  step reads is **protected**: writable only by the deterministic tier or a declared human, never by
  a proposal, under the revision policy's rules (no agent undoes a human's value). This closes the
  path from a model-read paragraph to a shell.

### 6. The engine and the steward

The hub is the **engine** for everything deterministic about ontological memory: the type registry,
write-boundary validation, versions and authority, spaces and their visibility, constraint conflicts,
promotion candidates, credit and decay. It already runs work of this kind (the pulse's sweeps, the
memory engine's credit and decay), and none of it calls a model.

Everything that needs a model (extraction, generalising a lesson for promotion, reconsolidation, the
residuals blind validator) is **work the hub files and a memory steward executes**:

- The steward is an actor with its own key, a `memory` capability (routed like `verify`), and a
  clearance for exactly one owner.
- The work is ordinary ASOPs (for example `memory-extract`, `memory-promote`,
  `memory-reconsolidate`), so their versions, outcomes and adjudications are measured like any
  procedure's, and the extractor improves through the same lessons loop.
- Extractor and validator are distinct roles (`constraints: distinct`). The hub's deterministic
  gates (schema, provenance present, the mechanical checker) decide what lands.
- The steward may ship in the same image and run beside the hub, so a single operator gets "the hub
  processes memory" with no setup. It is a deployment choice, not an architectural one: **no model
  credential lives in the process that holds the leases**.

### 7. Credit, on two axes

Trace reporting (`POST /memory/traces`) carries the facts a run read as well as the items it invoked.
The plane derives the outcome from the gate, as today. A verified success credits both the procedure
version and the facts read. A verified failure at a step is recorded against that step **and** flags
the facts that step read for reconsolidation. Over time, failures concentrated on one fact across
procedure versions point at the fact; failures concentrated on one version across facts point at the
procedure. That separation is the measurable form of orthogonality.

## What is NOT claimed

- **Orthogonality is a hypothesis.** Nothing yet shows that facts on entities beat facts in
  `common_mistakes`, or that failures can be attributed to one axis. The validation below exists
  to find out before most of this is built.
- **No parameter is validated**, as in 0006: promotion thresholds, convergence similarity and
  graduation length are engineering choices to be measured and recorded with their origin.
- **Convergence needs an equivalence test** between items written independently. A deterministic one
  (same subject and property, same value) is defined here; a semantic one for lessons is not.
- **Spaces do not protect secrets.** They are a relevance boundary inside one owner.

## Validation

In order of cost. Each can show the design is wrong.

1. **Census, no code.** Classify every `common_mistakes` entry in the authored ASOPs as procedural or
   factual. If factual entries are rare, the ontological axis solves a small problem; stop here.
2. **Ablation in the eval harness.** Add arms beside `asop_lesson`: procedure only; procedure plus
   facts on the entity; the same facts placed in `common_mistakes`. If facts on the entity do not beat
   facts in the lesson list on gate pass rate, the separation buys nothing at execution time.
3. **Fault localisation.** Seed a wrong fact, and separately a wrong procedure step. The design holds
   only if `plan_vs_actual` plus the credit records point at the right axis without a person reading
   the transcript.
4. **Staleness.** Change a real repository's test command. Count the runs that fail before the fact
   is corrected, and record which tier corrected it.
5. **Spaces and promotion in the network rig.** Two team spaces under one company space (one hub),
   and the same across hubs once 0007's memory channel exists: a fact used by both teams becomes a
   candidate by convergence; a team fact carrying a marker word never reaches another team's view or
   the company's; a company constraint flags a violating team fact.
6. **Injection.** A fact proposed from a change description is refused on any property a gate check
   reads, on every transport.
7. **Real runs.** None of the above means much without procedure-pinned runs from real work; the
   census and the ablation are the only steps that do not need them.

## Costs and consequences

- **Every item gets heavier again**: `space`, and on facts `subject` and `property`. Each is a field a
  migration carries on every backend.
- **The ASOP format grows a `facts` binding**, hub-local until the spec adopts it. A second
  implementation reading this plane's runs needs `sop_plan.facts` to reproduce one.
- **The type registry is operator work.** An entity type nobody declared cannot hold facts, which is
  safe and also the likeliest first-day confusion.
- **Approval queues grow** (proposed facts, promotions). Batch approval and graduation exist to keep
  them readable; if approvals start arriving faster than a person could read them, that is the
  revisit condition below.
- **The MCP surface stays at twelve tools.** New verbs ship on HTTP first; any MCP exposure is a
  decision under 0004's budget.

## Order of build

**Team spaces first (owner decision, 2026-10-30).** Spaces do not depend on the census or on entity
facts: they are useful to 0006 lessons as they stand, and they are the part of this record a team
using one hub feels first. The order below supersedes the original, which put spaces sixth.

Each step stands on its own and is useful without the next:

1. **Spaces** — **built**: `space` on every item (migration 0017; existing items read as the root);
   a reader sees its own space and every ancestor, on every read path (list, search, get, history,
   reviews, traces); writes land in the writer's own space only (`space_not_writable`); a declared
   human moves an item up or back down its own line (`POST /memory/{id}/space`, a new version,
   never sideways: `space_move_lateral`); a replica lands at the root. The actor's space comes from
   the key table's `"space"`.
2. The census (validation 1). No code.
3. Entity type registry; `subject` and `property` on items; validation at the write boundary;
   protected properties.
4. Resolution at `instantiate` into `sop_plan`, and `fact_unresolved`.
5. Runtime side: read resolved facts into the step brief; report facts read in traces.
6. Promotion candidates (type, demand, convergence), batch approval through step 1's move verb,
   constraint conflicts.
7. The steward capability and the first steward ASOP (`memory-extract`, proposed tier).
8. Across hubs, over 0007's memory channel, once that channel exists.

## Revisit condition

Revisit if the census finds factual entries rare; if the ablation shows no gain for facts on entities
over facts in lessons; if fault localisation cannot tell a wrong fact from a wrong step; if a
deployment needs a space boundary to protect secrets (that would mean the owner boundary is drawn
wrong); or if promotion approvals arrive faster than a person could have read them.
