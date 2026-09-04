# ASOP — Agentic Standard Operating Procedure

> *A standard operating procedure for one type of task, written as a sequence of
> beads, versioned as one artefact, executed by any harness, verified by a party
> other than the one that did the work, and revised from the evidence its own
> runs produce.*

**Status:** v3 — all seven review questions decided 2026-09-04 (§11). Supersedes `docs/asop.md` (v2).
**Home:** `packages/asop/` — the contract package both the Hub and the Harness import.
**Audience:** anyone building a harness, a plane, or a procedure. Nothing here names a
vendor, a company, or a tool.

---

## 0. What changed from v2, and why

v2 defined an ASOP as one procedure record with prose fields and a gate "declared at
authoring time." Two things turned out to be true of the shipped implementation that
the text did not say:

1. **One ASOP filed one work item.** A multi-step task was a chain of separate ASOPs
   linked by `next_sop` — a reading aid, not an executable structure. Nothing walked
   the chain; the steps could not be gated, attested or revised individually inside
   one versioned artefact.
2. **The gate was not on the procedure.** The record had no gate field; whoever filed
   work supplied the gate at filing time. Where the filer is on the executor's side —
   the ordinary case for a single-operator organisation — the executor's side authored
   its own gate, which is the failure mode the contract exists to prevent.

v3 fixes both by changing the grain: **the ASOP is the sequence; the step is what v2
called the procedure.** Everything v2 got right — the three properties, adjudication,
the revision policy, the enforcement model, the decomposition bounds — carries forward
unchanged and now applies per step.

---

## 1. Definition

An **ASOP** is a versioned, ordered sequence of **steps** that together accomplish one
**type of task**. Each step says what must be done, what must be true before starting,
how the result is proven, where the result is written, and which **role** does it.
Filing work from an ASOP produces a tree of beads — one per step — that any harness can
execute, and every bead pins the exact ASOP version it came from. When execution
departs from the procedure, the departure is adjudicated by a party other than the
executor and becomes the input to the next version.

It is a standard in the literal sense: the same procedure, the same version, the same
gates, run by different harnesses in different organisations, producing outcomes that
can be compared because they are counted against the same artefact.

### Running example

The document uses one procedure throughout:

```
ASOP  feature-dev  v3
  step 1  validate-requirements    role: analyst
  step 2  write-tests              role: implementer
  step 3  implement                role: implementer
  step 4  run-tests                role: implementer     gate: deterministic
  step 5  validate                 role: validator       gate: judged
                                   constraint: validator ≠ implementer
```

---

## 2. The three properties

A procedure is an ASOP when it has all three. Missing one, it is documentation.

### 2.1 Versioned — outcomes attach to versions, not to prose

Every revision of the ASOP is a distinct, immutable version. A run pins
`(asop_id, version)` and each of its beads pins `(asop_id, version, step)`. Outcomes
are recorded against that triple. "Did the v3 rewrite of step 2 help?" is answered by
counting, never by recollection.

*Corollary — template and instance are different things.* The ASOP never enters a work
queue. Runs do. A later version does not reach back into a run already filed; the run
finishes under the version it started under, and `drifted()` is how an in-flight run
learns its procedure has moved.

### 2.2 Verified — every step carries its own gate, authored with the version

A step's gate is part of the step, written by the ASOP's author, and immutable to
whoever executes or files it. It is one of three kinds:

| kind | proof | trust model |
|---|---|---|
| `deterministic` | a command exits 0, re-run fresh by the *completing* process | machine-checkable; the executor is its own attester, so this is a trust floor, not a proof |
| `judged` | a rubric fixed at authoring time, evaluated by a **route different from the executor's** | no self-grading |
| `human` | a named person acknowledges; the bead parks until they do | for irreversible or outward-facing steps |

The gate is enforced **where completion is recorded**, not where the work is done. A
run whose step cannot pass its gate did not complete that step, whatever the transcript
says. Every gate carries a park clock and an escalation path (§9); a gate with no
timeout is a deadlock.

### 2.3 Self-revising — divergence is input

When execution departs from a step, the departure is captured and **adjudicated** by a
party other than the executor:

- **good** — the procedure was wrong. Feeds the next version's `proposals`.
- **bad** — the execution took a shortcut. Feeds `common_mistakes` and root-cause
  analysis.

A plan-vs-actual review is written per step at the moment that step completes, while
the context still exists. Revision is deliberate: proposals accumulate, a pass drafts
the next version from them, a human or a policed agent activates it. Never silent.

---

## 3. Structure

### 3.1 The ASOP record

```yaml
asop_id: feature-dev          # stable identity across versions
version: 3                    # immutable once activated
title: Develop a feature
status: active                # draft | active | superseded | retired
task_type: feature            # the type of task this standardises (free label)
purpose: >
  Take a specified feature from requirements to verified, merged code.
trigger: >
  A feature bead exists with a written requirement and an owner.
inputs:                       # what a RUN must be given, by name
  - name: requirement
    description: the requirement document or bead the feature answers
  - name: repo
    description: the repository the feature lands in
    # kind: reserved, optional, unvalidated in v3 — see §11.5
roles:                        # declared here, bound by the harness (§7)
  analyst:     {kind: agent}
  implementer: {kind: agent}
  validator:   {kind: agent}
constraints:
  - distinct: [implementer, validator]   # the same binding may not fill both
steps: [...]                  # §3.2, ordered
author: mabidoli
author_kind: human            # human | agent — set by the operator, never inferred
proposals: []                 # open revision proposals, carried until addressed
superseded_by: null
created_at: 2026-09-04T00:00:00Z
```

**Bounds.** An ASOP has at most **7 steps** at one level and nests at most **3 deep**
(§3.5). These are the decomposition bounds the plane already enforces on bead trees,
and they mean the same thing here: a procedure sized to what an accountable person can
sanity-check. A procedure that needs more is usually two procedures.

### 3.2 The step

A step is what v2 called the procedure. Its fields are unchanged in meaning; what is new
is that a step belongs to an ASOP, carries a gate, and names a role.

```yaml
- step: 5
  name: validate
  role: validator
  purpose: >
    Confirm the implemented feature satisfies the requirement as written, not as
    interpreted.
  entry_check: >
    Step 4 is done with its gate passed; the requirement from the run's inputs is in
    hand; the diff is available.
  inputs: >
    The requirement, the diff, the test report from step 4.
  definition_of_done: >
    Every acceptance criterion in the requirement is traced to a test that exercises
    it and passes, or is explicitly listed as not covered with a reason.
  validation: >
    A traceability table exists mapping each criterion to a test id; no row is empty.
  write_back: >
    The traceability table is attached to the run's parent bead; gaps are filed as
    sibling repair beads against step 3.
  common_mistakes:
    - Validating against the implementation's own description of itself.
    - Marking a criterion covered because a test with a similar name exists.
  gate:                        # §3.3 — authored HERE, with the version
    kind: judged
    check: >
      Every acceptance criterion maps to a passing test id, or to a stated gap.
    rubric: asop://feature-dev/v3/rubrics/validate
    judge_route: distinct-from-executor
    max_park_seconds: 86400
    on_timeout: escalate
    escalate_to: role:owner
  after: [4]                   # §3.4 — ordering; default is the previous step
  tags: []
```

Field semantics, carried from v2:

- `entry_check` — what must be true and in hand *before* starting, phrased so a missing
  item becomes a question to ask rather than an assumption to make.
- `definition_of_done` and `validation` are kept distinct on purpose: the first is
  what is being *claimed*, the second is the check that would *fail if the claim were
  false*. Collapsing them is how "done" comes to mean "I believe I finished."
- `write_back` — where the outcome lands, for whom. The half of a handoff that gets
  dropped, and the half the next step's `entry_check` is written against.
- `common_mistakes` — grows from bad adjudications (§6).

### 3.3 The gate

The gate schema is the one `asop.gates.validate_gate` already enforces (schema v1),
unchanged. It has three groups:

| group | fields | notes |
|---|---|---|
| core | `kind`, `check` **or** `checks[]` | `checks` is a staged ladder; a failure names the stage. `class` is accepted as a read-alias of `kind` for records written before the schema was unified |
| clock | `max_park_seconds`, `on_timeout` (`pass` \| `fail` \| `escalate`), `escalate_to`, `verifier` | all-or-nothing as a group. A `human` gate must name its `verifier`; a `deterministic` gate must not |
| execution | `cwd`, `timeout_s`, `rubric`, `judge_route` | hints to the executing domain |

Whether the clock group is mandatory is a caller decision (`require=("clock",)`): a
coordination plane requires it, because a parked gate it cannot expire is a queue it
cannot drain; a runtime may relax it for deterministic gates that cannot park.

**What v3 changes about gates is only where they live:** on the step, authored with the
version. A run supplies none. A harness that lets a filer or executor override a step's
gate is not conforming.

### 3.4 Ordering

Steps are a sequence by default: step *n* is `after: [n-1]`. A step may declare `after`
explicitly to run in parallel with siblings — the tree it files carries that as
`blocked_by`. The bound on width is the bound on steps. `after` is validated at
`sop_create`: a reference to a step that does not exist, or a cycle, is `sop_refused`.

### 3.5 Nesting

A step may be an ASOP: `uses: {asop_id, version}` in place of a body. Filing it files
the inner ASOP's tree as that step's children, pinned to the inner version. Depth counts
against the 3-deep bound. The inner ASOP's gates are its own; the outer step's gate, if
any, applies to the inner run as a whole.

### 3.6 Roles and separation of duties

An ASOP names **roles**, never agents, models, vendors or people. `implementer` and
`validator` are role names; what fills them is a harness decision at run time (§7).
This is what makes the artefact shareable: two organisations run `feature-dev v3` with
different bindings and their outcomes are still counted against the same version.

`constraints` express separation of duties over roles. `distinct: [a, b]` means the
binding that fills `a` in a run may not fill `b` in that run. The contract requires one
constraint by construction: **a step whose gate is `judged` is evaluated by a route
distinct from the route that executed it**, and a step of role `validator` over another
step's output may not share a binding with that step's role. A harness that cannot
satisfy a constraint **refuses the run** (`role_unbound`, `constraint_unsatisfiable`) —
it does not quietly bind the same agent twice.

Roles have a `kind`: `agent` or `human`. A `human` role's steps carry `human` gates by
construction, and so does a step carrying a **protected tag** (`money`, `irreversible`
by default — §6.4): the tag means a person looks before the step counts as done, and
the record refuses a gate that means nobody does. The revision policy protects both
classifications.

---

## 4. Lifecycle of the artefact

```
create ──▶ DRAFT ──activate──▶ ACTIVE ──(a later version activates)──▶ SUPERSEDED
                                  │
                                  └──retire──▶ RETIRED
```

- **DRAFT** — being written. Cannot be run. Every field may change.
- **ACTIVE** — exactly one version of an `asop_id` is active at a time. Immutable.
  Runs file from it.
- **SUPERSEDED** — replaced by a later active version. Kept forever: runs pinned to it
  must stay resolvable or their outcomes become unreadable.
- **RETIRED** — withdrawn with no successor. Existing runs finish under their pin; no
  new runs file. Human-only: an agent may not withdraw a procedure, in the same spirit
  as the ratchet rule.

`revise` never edits a version. It creates a new DRAFT from an existing version plus a
change set, and records who made it and whether they were human. Activation of a
revision is policed when the reviser is an agent (§6.4).

---

## 5. Lifecycle of a run

A **run** is one execution of one ASOP version against concrete inputs.

### 5.1 Filing

`run` takes `(asop_id, inputs)` and files a tree:

```
parent bead        pins (asop_id, version); carries the run's inputs
  ├─ step 1 bead   pins (asop_id, version, 1); gate from step 1; blocked_by: []
  ├─ step 2 bead   pins (…, 2); blocked_by: [step 1]
  ├─ …
  └─ step 5 bead   pins (…, 5); blocked_by: [step 4]
```

Rules, all enforced at filing:

- The version must be ACTIVE. A draft refuses (`sop_refused`): generating work from an
  unactivated procedure hands somebody a half-written instruction with the authority of
  a published one.
- Every declared input is supplied, or the run refuses (`inputs_missing`).
- Every role resolves to a binding satisfying every constraint, or the run refuses
  (`role_unbound`, `constraint_unsatisfiable`).
- Each step bead carries a **copy** of its step's text and gate, pinned. The
  plan-vs-actual review reads the words the executor was actually handed, even after
  the procedure moves on.
- The child is written into the parent's `blocked_by` in the same lock that files it.
  A parent cannot close while a child is open.
- Decomposition bounds apply to the tree as filed (`decomposition_bound`).

### 5.2 Executing a step

Any harness may claim a step bead its binding is eligible for. Claims are fenced leases:
a claim carries an attempt number, and a report against a stale attempt is refused
(`not_the_holder`). The executor reads the step's text, the run's inputs, and the
outputs of the steps it is `after`.

Completion is **requested, not declared**. The executor reports a result; the store
that owns the bead runs the gate at its own status flip:

| gate outcome | bead status | releases dependents? |
|---|---|---|
| passed | `done` | yes |
| `deterministic` / `judged` failed | `verify_failed` | **no** |
| `human` parked | `awaiting_verify` | **no** |
| park clock expired | per `on_timeout`: `done` / `verify_failed` / escalated | as above |

Neither `verify_failed` nor `awaiting_verify` releases anything downstream. This kills
the momentarily-done race.

### 5.3 Attestation

Where the store and the plane are different trust domains, the executing domain submits
an **attestation** — `check`, `exit_status`, `environment`, `at`, `submitted_by` — and
the plane verifies and stores the claim. It does not run commands. A `judged` or
`human` gate's attestation names the judge or the person; a `deterministic` gate's
attestation names the executor, and every reader of it knows that is a claim.

### 5.4 Failure and repair

A failed step **keeps its failed status** — still blocking everything after it — until
its own gate is re-run and passes. Repair is a sibling **repair bead** (`repairs:
<step bead>`), filed beside, never beneath. It does not count against the parent's
budget: the red original is what holds the parent open. First failure files one repair
bead; second failure escalates to a human; there is no third autonomous attempt.

### 5.5 Completion

The run's parent bead closes when every step bead is `done`. At that moment:

- a **plan-vs-actual** review is written for the run, from the per-step reviews;
- the run's outcome is recorded against `(asop_id, version)` and each step's against
  `(asop_id, version, step)`;
- open adjudications on the run are queued for the lessons pass (§6).

---

## 6. Self-improvement

### 6.1 Divergence and adjudication

*Divergence* is the phenomenon — execution departed from the step. *Adjudication* is the
record that judges it: `good` or `bad`, with the adjudicator's identity and pointed
evidence. The adjudicator **must not be the executor** (`adjudication_self`), must
adjudicate an executed step (`adjudication_unexecuted`), and adjudicates once
(`adjudication_exists`).

In a single-operator organisation the adjudicator is the operator, or a route the
operator has **declared** as an adjudicator and that is distinct from the executor's.
Declared, never inferred: a registry with no declared adjudicator makes the operator the
only one. A harness where the only possible adjudicator is the executor's own route has
no self-improvement loop, and its ASOPs are documentation.

### 6.2 Plan-vs-actual

Written at the moment a step completes, comparing the step text the executor was handed
against what the executor reports having done. It is evidence for adjudication, not an
adjudication.

### 6.3 Lessons and proposals

`propose` is the cadence pass. It reads adjudications nobody has proposed yet and drafts
the next version: good ones into the step's `proposals`, bad ones into the step's
`common_mistakes`. It may also propose **structural** changes — a step split, a step
added, an ordering changed — when the evidence is the same divergence recurring at the
same boundary. A draft, never an activation.

`outcomes_by_version` is the other half: per version and per step, counts of `done`,
`verify_failed`, escalations, repairs, mean park time. Whether a revision helped is a
comparison of two rows.

### 6.4 Revision policy

When the reviser is an agent, three rules apply, computable because versions are
immutable and each records who authored it:

1. **Protected tags freeze a step against agents.** A step tagged `money` or
   `irreversible` (operator-extensible, never operator-reducible) may not be changed by
   an agent, nor may an agent add or remove such a tag.
2. **Classification ratchets toward human, never away.** An agent may move a role or a
   gate to `human`; only a human may move it back.
3. **No undoing a human.** An agent may not move any field into a state a human moved it
   away from, until a human moves it back.

Who is human is **declared by the operator**, never inferred from a name. An undeclared
registry polices everyone. A human reviser is bound by none of these. Activation is
policed the same way, or the policy has a door beside it.

### 6.5 Promotion — where new ASOPs come from

Not every goal needs an ASOP. A harness's planner decomposes one-off goals into bead
trees ad hoc; that is the ordinary case and it stays. An ASOP is for a **recurring type
of task**.

`promote` takes a completed run tree — a planner's decomposition that worked, with its
gates and its adjudications — and drafts an ASOP from it: the beads become steps, the
executors become roles, the verify bead becomes the validator step. This is the front
door of the loop: the standard grows from evidence that a shape works, not from a blank
page.

In v3 a human opens that door. Promotion is refused when an active ASOP already covers
the run's `task_type`; the variant becomes a new version through `revise`. A later
version of this contract lets agents promote to a draft and lets a harness auto-promote
past a threshold it configures (§11.7) — activation stays human either way.

---

## 7. Sharing across harnesses

What is **portable** — carried in the artefact, identical everywhere:

- the ASOP and its steps, gates, roles, constraints, bounds;
- the version pin and its semantics;
- the refusal vocabulary (§10) and the status vocabulary (§5.2);
- the adjudication record and the attestation record.

What is **local** — decided by each harness, never in the artefact:

- **role bindings** — which agent, model, route, or person fills each role;
- the work-unit store and its transport;
- how a `judged` route is chosen, provided it is distinct from the executor's;
- who the operator declares human;
- schedule, cost, and capacity.

A **plane** stores ASOPs, versions them, records outcomes per version, routes human
gates and adjudications, runs the lessons pass across every harness that reports to it,
and is otherwise advisory: it never executes a check and never blocks a harness.

A **harness** runs standalone against a local ASOP store with the same contract. When a
plane is configured, it pulls ASOPs from it, reports runs and outcomes to it, attests to
it — and never publishes without a human-visible prompt.

A harness is **conforming** when it passes the conformance suite in this package against
its own store: same verbs, same refusals, same statuses, same pin semantics.

---

## 8. Verbs

The contract, transport-neutral. Every verb names who may call it, its preconditions,
its effect, and the refusals it can return. HTTP and MCP encodings map onto these one
to one.

### 8.1 Authoring

| verb | caller | preconditions | effect | refusals |
|---|---|---|---|---|
| `sop_create(asop)` | human or agent | fields valid; not empty; bounds hold | new `asop_id` at v1, DRAFT | `sop_refused`, `decomposition_bound` |
| `sop_revise(asop_id, from_version, changes)` | human or agent | source version exists | new DRAFT version; records author, author_kind | `sop_refused`, `version_required`, `revision_policy:<rule>` |
| `sop_activate(asop_id, version)` | human, or agent under policy | version is DRAFT | version ACTIVE; previous active SUPERSEDED | `sop_refused`, `revision_policy:<rule>` |
| `sop_retire(asop_id)` | human | an active version exists | active version RETIRED; new runs refuse | `sop_refused`, `revision_policy:human_only` |
| `sop_get(asop_id, version?)` | any | — | the version, or the active one | — |
| `sop_list(filter?)` | any | — | ids with active version and status | — |
| `sop_history(asop_id)` | any | — | every version, oldest first | — |

### 8.2 Running

| verb | caller | preconditions | effect | refusals |
|---|---|---|---|---|
| `run(asop_id, inputs, version?)` | human or agent | version ACTIVE; inputs complete; roles bindable | files the tree (§5.1); returns run id | `sop_refused`, `inputs_missing`, `role_unbound`, `constraint_unsatisfiable`, `decomposition_bound` |
| `run_get(run_id)` | any | — | the tree with statuses and pins | `work_item_unknown` |
| `run_list(asop_id?, status?)` | any | — | runs, newest first | — |
| `drifted(run_id)` | any | — | whether the pinned version is no longer active | — |

### 8.3 Executing

| verb | caller | preconditions | effect | refusals |
|---|---|---|---|---|
| `work_claim(bead_id, ttl)` | a binding eligible for the step's role | bead ready; no live lease | fenced lease with attempt number | `work_conflict`, `capability_mismatch`, `bad_ttl` |
| `work_report(bead_id, attempt, result)` | the lease holder | attempt is current | store runs the gate at the flip (§5.2) | `not_the_holder`, `attempt_required`, `attestation_required` |
| `work_attest(bead_id, attestation)` | the executing domain | bead reported | plane verifies and stores the claim | `attestation_invalid` |
| `verify_approve(bead_id)` / `verify_reject(bead_id, reason)` | the named `verifier` | bead is `awaiting_verify` | `done` / `verify_failed` | `not_terminal`, `unauthenticated` |
| `repair(bead_id)` | human or agent | bead is `verify_failed` | files a sibling repair bead (§5.4) | `decomposition_bound` |

### 8.4 Improving

| verb | caller | preconditions | effect | refusals |
|---|---|---|---|---|
| `adjudicate(bead_id, good\|bad, evidence)` | anyone but the executor | bead executed; none exists | adjudication recorded | `adjudication_self`, `adjudication_exists`, `adjudication_unexecuted`, `adjudication_invalid` |
| `propose(asop_id?)` | human, or a scheduled pass | unproposed adjudications exist | drafts the next version (§6.3) | `sop_refused` |
| `outcomes(asop_id)` | any | — | per-version and per-step counts | — |
| `promote(run_id)` | human (v3) | run complete; no active ASOP for its `task_type` | drafts an ASOP from the tree (§6.5) | `decomposition_bound`, `sop_refused` |

---

## 9. Enforcement model

Unchanged from v2 Part III; restated per step.

**Trust domains.** Gate enforcement happens inside the domain that owns the bead — the
store that flips its status — never in a shared plane. A plane stays advisory and
verifies attestations.

**Execution contract for `deterministic` checks.** Per gate, an implementation pins:
identity (which principal runs the check), environment (working directory and permitted
secrets — no ambient credentials), isolation (the check cannot rewrite the record it
verifies), timeout (mandatory; expiry is failure, never a hang), idempotency (re-running
a passed gate is safe).

**Liveness.** Every `human` gate declares `max_park_seconds` and `escalate_to`. Silence
past the deadline resolves by `on_timeout` — decided by omission is an outcome; an
ignored queue is not. A quarantine pass surfaces stalled gates on a digest.

**The re-verify invariant.** Repair never substitutes for the work it repairs (§5.4).

**Bounds.** §3.1. Recursion is how the bound is honoured, not how it is broken.

---

## 10. Refusal vocabulary

Refusals are the contract's error language: a stable code, a human sentence, and no
stack trace. Codes relevant to ASOPs, from `asop.refusals`, plus the ones v3 adds:

| code | meaning |
|---|---|
| `sop_refused` | the ASOP or version cannot be used as asked (draft run, empty record, bad field) |
| `version_required` | an operation that needs a version got only an id |
| `revision_policy:<rule>` | an agent revision broke `protected`, `ratchet` or `no-undo` |
| `decomposition_bound` | more than 7 children or deeper than 3 |
| `adjudication_self` / `_exists` / `_unexecuted` / `_invalid` | §6.1 |
| `work_conflict` / `not_the_holder` / `attempt_required` | fenced-lease rules |
| `capability_mismatch` | the caller's binding is not eligible for the step's role |
| `attestation_required` / `attestation_invalid` | §5.3 |
| `not_terminal` | a verdict on a bead that is not parked |
| `metadata_reserved` / `natural_key_reserved` | a caller wrote a plane-owned key |
| **`inputs_missing`** *(v3)* | a declared run input was not supplied |
| **`role_unbound`** *(v3)* | no binding for a role |
| **`constraint_unsatisfiable`** *(v3)* | every binding violates a `distinct` constraint |

---

## 11. Open questions for this review

1. **Naming.** ~~Open~~ **DECIDED 2026-09-04: container = `ASOP`, unit = `Step`, check =
   `gate`.** `asop.sop.SOP` is renamed to `Step`. Grounded in doctrine: the US Army's
   Training & Evaluation Outline decomposes a *task* (conditions, standards) into
   *performance steps*, each with *performance measures* rated GO/NO-GO — a one-to-one
   map onto ASOP / Step / gate. "Step" is also the unit in ISO 9001 work instructions,
   fire-service SOPs and police procedures; aviation's "item" is a checklist line, not an
   activity with an owner and a proof. (Vault: `1 - Projects/AgentCo/Research/2026-09-04
   SOP terminology across domains`.)
2. **Ordering.** **DECIDED 2026-09-04: ship `after` in v3.** Linear is the default
   (`after: [n-1]`); a Step may declare `after` explicitly to run in parallel with
   siblings, and the tree carries it as `blocked_by`. Validated at `sop_create`: a
   dangling reference or a cycle is `sop_refused`. Width is bounded by the step count.
3. **`retired`.** **DECIDED 2026-09-04: add the state.** `sop_retire` is human-only,
   moves the active version to `retired`, refuses new runs (`sop_refused`), lets
   in-flight runs finish under their pin, and keeps the record forever like
   `superseded`. The Army rescinds SOPs and ISO obsoletes documents; both keep the
   record.
4. **`next_sop`.** **DECIDED 2026-09-04: dropped.** Composition inside the contract is
   nesting (§3.5) — pinned, gated, bounded. Anything above that — sequencing one ASOP
   after another, choosing which procedure a situation calls for, a process of
   processes — is **orchestration, and orchestration belongs to the harness**, not to
   the artefact. The contract describes a procedure; the runtime decides when to run
   one. (mabidoli: "we may need a higher orchestration layer, but that is delegated to
   the harness that is using the ASOPs.")
5. **Run inputs.** **DECIDED 2026-09-04: names plus free descriptions in v3.** The
   contract checks presence (`inputs_missing`); what an input *is* is between the
   author and the harness. `kind` is reserved on the input record — optional and
   unvalidated — so that when a second harness disagrees with the first about the
   shape of an input, the disagreement can be named without a schema change. Kinds
   get standardised from that evidence, not guessed now.
6. **Who adjudicates in a one-person organisation.** **DECIDED 2026-09-04: a declared
   route may adjudicate.** The contract forbids exactly one thing — self-adjudication —
   and keeps it. A route may adjudicate an agent's step when it is distinct from the
   executor's route **and the operator declared it as an adjudicator**; declared, never
   inferred, the same way verifiers are. A registry with no declared adjudicator makes
   the operator the only one — the human-only posture is the default, the route is the
   opt-in. Activation stays human regardless: an agent-adjudicated divergence becomes
   a proposal, and a person activates the version it produces.
7. **Promotion authority.** **DECIDED 2026-09-04: humans only in v3.** `promote` is a
   human verb. It drafts an ASOP from a run tree, and is refused when an active ASOP
   already covers the same `task_type` — the path for a variant is a new *version* via
   `revise`, not a second draft.

   **Reserved for a later version:** agents may `promote` to a draft, and a harness may
   set a threshold — *N* clean completions with no bad adjudication — that auto-promotes
   a new ASOP, or a new version of an existing one, for a human to validate. The
   threshold is a harness number, not a contract number. The line that does not move:
   agents may draft, only humans activate.

---

## 12. What an ASOP is not

- **Not a prompt file.** Prose without a gate is advice.
- **Not a workflow engine.** The ASOP executes nothing; harnesses do.
- **Not an orchestrator.** An ASOP does not say what runs after it, or which procedure
  a situation calls for. Composition *inside* one procedure is nesting; sequencing
  *between* procedures is the harness's decision and lives in its configuration.
- **Not a straitjacket.** Divergence is expected and metabolised, not forbidden.
- **Not every goal.** One-off work stays with the planner; ASOPs are for recurring
  types of task.
- **Not a place to name agents.** Roles live in the artefact; bindings live in the
  harness.

---

## 13. Glossary

| term | meaning |
|---|---|
| **ASOP** | the versioned sequence of steps for one type of task |
| **step** | one unit of the sequence: text, gate, role |
| **gate** | how a step proves it is done: `deterministic`, `judged`, `human` |
| **role** | a named responsibility in the ASOP, bound to an executor by the harness |
| **binding** | what fills a role in a run: an agent, a route, a person |
| **run** | one execution of one ASOP version against concrete inputs |
| **bead** | the work unit; a run is a tree of them, one per step |
| **pin** | `(asop_id, version)` on a run, `(asop_id, version, step)` on a bead |
| **attestation** | a proof-of-execution record the executing domain submits to a plane |
| **divergence** | execution departed from the step |
| **adjudication** | the record judging a divergence `good` or `bad`, by a non-executor |
| **repair bead** | a sibling filed to fix a `verify_failed` step; never substitutes |
| **promotion** | drafting an ASOP from a run tree that worked |
| **plane** | a coordination layer storing ASOPs and outcomes across harnesses |
| **harness** | a runtime that executes beads; runs alone or reports to a plane |

---

*Provenance: v1 authored 2026-08; v2 adversarially reviewed and cross-validated by three
frontier-model vendors 2026-08-31 (unanimous "adapt"; must-fixes became the enforcement
model). v3 drafted 2026-09-04 from a review that found the shipped grain and gate
placement did not match v2's text. Prior-art review 2026-08-31 (AWS Agentic SOPs/Strands,
Decagon AOPs, Skan, Agent-S): instruction documents all — none carry per-version
outcomes, embedded gates, or divergence-driven revision.*
