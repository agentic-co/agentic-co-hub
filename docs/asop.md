# ASOP — Agentic Standard Operating Procedure

> *An SOP tells an agent what to do. An ASOP can prove it was done — and gets better
> when it wasn't.*

This document is the canonical definition: first the contract (theory), then the
reference implementation (practice, as **beads** — the work-unit our production hub has
run since 2026-08).

---

## Part I — Theory: the contract

A procedure qualifies as an ASOP when it satisfies three properties. Miss any one and
you have documentation, not an operating procedure.

### 1. Versioned — outcomes attach to versions, not to prose

Every revision of the procedure is a distinct, immutable version. Work executed under a
procedure **pins the exact version it ran under**, and outcomes are recorded per
version — counts, not impressions. "Did the v4 rewrite help?" is answered by
`outcomes_by_version`, never by whoever remembers loudest.

*Corollary — the template/instance split:* the ASOP itself never enters a work queue.
Instances pin `(asop_id, version)`; the template evolves independently. Attributing an
outcome to "the SOP" rather than "v3" would credit text that has since changed.

### 2. Verified — the definition of done travels with the procedure

An ASOP carries its own gate, declared at authoring time, in one of three classes:

| Class | Proof | Trust model |
|---|---|---|
| `deterministic` | a command exits 0, re-run fresh by the *completing* process | machine-checkable |
| `judged` | a rubric fixed at authoring time, evaluated by a route **different from the executor's** | no self-grading |
| `human` | a named person acknowledges; the work parks until they do | irreversible/outward actions |

The gate is enforced **where completion is recorded, not where work is executed** — an
executor that grades its own homework is the failure mode this contract exists to kill.
A run that cannot pass its gate did not complete, whatever the transcript says.

Two clauses keep the gate honest: **the gate's author must not be its executor** — a
party that writes its own check can write a tautology, so gates are authored at plan
time by the planning authority and are immutable to the executor; and **every gate
carries liveness semantics** (Part III) — a gate with no timeout is a deadlock, not a
control.

### 3. Self-revising — divergence is input, not embarrassment

*Naming, because one word was doing two jobs:* **divergence** is the
phenomenon — execution departed from the procedure. **Adjudication** is the
record that judges it, and it is what implementations carry as a field. The
coordination plane already uses `divergence` for something unrelated (a
snapshot's pointer moving), so the ASOP concept is `adjudication` everywhere it
becomes code.

When execution departs from the procedure, the divergence is captured and tagged:

- **good divergence** — the procedure was wrong; the deviation feeds the next version.
- **bad divergence** — the execution took a shortcut; it feeds root-cause analysis.

The tag is an *adjudication, not a confession*: whoever tags divergence must be a
different party from the executor whose fault a "bad" tag would admit, and the tag
carries the adjudicator's identity and pointed evidence.

A plan-vs-actual review is generated **at the moment of completion** (while the context
still exists), and revision proposals accumulate against the template. The loop closes
on a cadence — captured per-run, revised deliberately, never silently.

Revision is itself policed when the reviser is an agent. Three rules, generic to
every registry and computable because versions are immutable and each records who
authored it: a step carrying a protected tag (`money` and `irreversible` by
default) is frozen against agents, who may neither change it nor add or remove
such a tag; a step's class may ratchet toward human but never away from it; and an
agent may not move any field into a state a human moved it away from, until a
human moves it back. Who is human is declared by the operator, never inferred, and
an undeclared registry polices everyone. A human reviser is bound by none of these.
Activation is policed the same way, or the policy has a door beside it. Without
the policy, self-revision is an unpoliced write path into the procedures — the one
shape a harness with production access cannot accept.

### What an ASOP is not

- Not a prompt file: prose without a gate is advice.
- Not a workflow engine: the ASOP does not execute anything; harnesses do.
- Not a straitjacket: divergence is expected and *metabolized*, not forbidden.

---

## Part II — Practice: the bead implementation

The reference implementation runs in the AgentCo hub runtime, where the unit of work is
a **bead** — a JSONL record that carries everything Part I requires. AgentCo (this
repository) is the coordination plane that stores ASOPs, versions them, records
outcomes, and routes human gates; any harness's work-unit can execute them. Beads are
ours.

### Anatomy of a gated bead

```json
{
  "id": "ac-8dd04b6e",
  "title": "F1: ingestion core — parsers → normalized ledger",
  "parent_id": "ac-984af7ae",
  "blocked_by": ["ac-1bf072c5"],
  "status": "pending",
  "metadata": {
    "epic": "household-finance",
    "sop": {"asop_id": "sop-ingest", "version": 3},
    "verify": {"class": "deterministic",
                "check": "uv run pytest tests/ -q",
                "cwd": "/path/to/repo"},
    "context_refs": [{"path": "docs/CONCEPT.md", "why": "design contract"}]
  }
}
```

The field names inside `verify` above (`class`, `cwd`) are the bead runtime's own
schema, not this repository's. This repository's `validate_gate`
([`agentco/gates.py`](../agentco/gates.py)) takes `kind`, `check`,
`max_park_seconds`, `on_timeout`, `escalate_to`, and `verifier`, and refuses
`class` or `cwd` as unknown fields — so copying this example straight into a
`work_create` call here gets refused, not accepted.

Every Part-I property has a concrete home:

| Contract | Bead mechanics |
|---|---|
| Versioned | `metadata.sop` pins `(asop_id, version)`; the plane's `outcomes_by_version` aggregates results per version |
| Verified | `metadata.verify` — validated at the **write boundary** (malformed gates are rejected, never silently no-op'd); enforced at the store's DONE flip. For `judged` and `human` gates that means no executor path can self-grade. A `deterministic` gate is the deliberate exception: its executor IS its own attester, so its "proof" is a submitted claim about a check the plane never re-runs — a trust floor, not a proof, and every description of attestation has to say so (`docs/connection-harness.md` § Risks). On this repository's coordination plane a `human`-class gate must additionally name who answers it — the `verifier` field (`agentco/gates.py`) — because without one the routed work item has no assignee and requires no capability, and the queue offers it to the executor: the one party a human gate exists to exclude. A `deterministic` gate must not name one; its executor is its own attester. |
| Self-revising | goal-close auto-writes a plan-vs-actual review; fix-beads carry `metadata.adjudication: good\|bad`; good feeds template revision, bad feeds RCA |

### The lifecycle around the bead (PPEV)

1. **Prime** — a content-stamped context cache per project (git HEAD + doc hashes;
   extractive pointers only) is injected into every execution, plus the bead's own
   `context_refs`. No run starts cold.
2. **Plan** — a goal decomposes into ≤6 work beads + 1 verify bead created *up front*,
   `blocked_by` all siblings — ready by construction, no event system required.
   Ordering is `blocked_by`; bulk grouping is `epic`; fix-beads are siblings, never
   children.
3. **Execute** — any harness works the bead. Completion is requested, not declared.
4. **Verify** — the gate runs where the status flips. Two extra statuses make gates
   first-class: `awaiting_verify` (human gate parked, assigned to the named person —
   nothing *pushes* it to them unless the opt-in write-back connector,
   [`docs/writeback.md`](writeback.md), is configured; absent that, reaching them means
   reading the change feed or polling the queue) and `verify_failed` (retryable;
   **neither status releases downstream `blocked_by`**,
   killing the momentarily-done race). First failure spawns one sibling fix-bead;
   second failure escalates to a human — never a third autonomous attempt.

### Operational clauses proven in production

- **Stuck-gate quarantine:** a bead in `awaiting_verify` beyond N days moves to a weekly
  digest — abandonment degrades to silence, not queue noise.
- **Legacy scope guard:** gates apply only where a `verify` payload (or ASOP lineage)
  exists; thousands of pre-contract items keep legacy semantics. No backfill, no flood.
- **Watchdogs at the seam:** silence-based idle timeout and an explicit completion
  marker (`AGENTCO_DONE:`) make "agent went quiet" a handled state, not a discovery.

---

## Part III — Enforcement model

Added in v2, after a three-vendor adversarial review (GPT-5.5, Gemini 3.1 Pro, GLM-4.7 —
unanimous "adapt") converged on one gap: v1 named the gates but left *who may run them,
where, and for how long* as prose. That is the dangerous part, so it is now contract.

### Trust domains — who enforces what

Gate enforcement happens **inside the trust domain that owns the work record**, never in
a shared coordination plane:

- A **work-unit store** (like the bead runtime) enforces gates at its own status flip.
  That store is a system of record *for its owner* — gating its own flip is local
  policy, exactly as a git host enforces required status checks.
- A **coordination plane** (AgentCo) stays advisory: it never executes a check and never
  blocks a harness. For plane-level assurance the contract is **attestation**: the
  executing domain submits a proof-of-execution record (check identity, exit status,
  environment fingerprint, timestamp, submitter identity) and the plane *verifies and
  stores the claim* — it does not run commands. "Advisory, never blocking" and
  "gate at the flip" are statements about different layers; v1 blurred them.

### Execution contract for deterministic checks

A conforming implementation must pin, per gate: **identity** (which principal runs the
check), **environment** (working directory and permitted secrets — a check receives no
ambient credentials), **isolation** (the check cannot rewrite the record it is
verifying), **timeout** (mandatory; expiry = failure, never a hang), and **idempotency**
(re-running a passed gate must be safe).

### Liveness — gates must terminate

- Every human gate declares a **maximum park time and an escalation path** at authoring
  time. Silence past the deadline resolves by the gate's **declared default** — decided
  by omission is an outcome, an ignored queue is not.
- Parked and failed verify states never release dependents, but they **escalate on a
  clock**: quarantine surfaces stalled gates on a periodic digest, and the
  default-with-deadline closes them. Correctness without liveness is a deadlock with
  good intentions.

### Failure and repair — the re-verify invariant

A fix item **never substitutes** for the work it repairs. The failed unit keeps its
failed status — still blocking everything downstream of it — until **its own gate is
re-run and passes**. Repair restores the original's ability to prove itself; it does not
vouch for it. (Fix items are filed beside, not beneath, to keep repair depth bounded.)

### Decomposition bounds

The ≤6-work-units-plus-verify budget is not a law of nature; it is a **human review
bound** — a decomposition sized to what the accountable person can actually sanity-check.
Implementations may raise it, but the escape hatch is explicit: a goal that genuinely
needs more is usually two goals.

The coordination plane enforces it at create: `metadata.parent` makes a work item a
child; a parent holds at most seven (six plus the verify unit; `AGENTCO_MAX_CHILDREN`
is the explicit escape hatch), a tree goes at most three deep, and the child is
written into its parent's `blocked_by` in the same lock that files it, so a parent
cannot close while a child is open. Recursion is how the bound is honoured, not how
it is broken. A repair (`metadata.repairs`) goes beside the unit it repairs — same
parent or none, never beneath — and counts against nobody's budget: the red
original is what holds the parent open.

### Explicitly not covered (yet)

ASOP v2 does not specify **rollback/compensation** (a gate that passed on work later
found wrong triggers no automatic undo — pair with saga/compensation patterns where
consequences are transactional) or **dispute arbitration** between executor and
adjudicator beyond "escalate to the routing spine." Claiming otherwise would repeat v1's
mistake.

### On novelty, plainly

Most ASOP ingredients exist elsewhere — required status checks in CI, Temporal's
determinism, BPMN compensation, ITIL's improvement loops. The unclaimed part is the
**combination under one contract**: a gate bound at authoring time, enforced where the
outcome is recorded, with outcomes accounted per version and divergence feeding
revision. ASOP names that contract; it does not claim a new computing primitive.

*Provenance: contract adversarially reviewed (architecture + operations lenses) before
implementation, then cross-validated by three frontier-model vendors (2026-08-31, unanimous
"adapt" — their must-fixes became Part III); gate placement moved from executor to store as a result. Prior-art
review 2026-08-31 (AWS Agentic SOPs/Strands, Decagon AOPs, Skan, Agent-S): instruction
documents all — none carry per-version outcomes, embedded gates, or divergence-driven
revision. See `decisions/asop.md`.*
