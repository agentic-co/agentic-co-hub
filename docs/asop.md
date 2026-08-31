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

### 3. Self-revising — divergence is input, not embarrassment

When execution departs from the procedure, the divergence is captured and tagged:

- **good divergence** — the procedure was wrong; the deviation feeds the next version.
- **bad divergence** — the execution took a shortcut; it feeds root-cause analysis.

A plan-vs-actual review is generated **at the moment of completion** (while the context
still exists), and revision proposals accumulate against the template. The loop closes
on a cadence — captured per-run, revised deliberately, never silently.

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

Every Part-I property has a concrete home:

| Contract | Bead mechanics |
|---|---|
| Versioned | `metadata.sop` pins `(asop_id, version)`; the plane's `outcomes_by_version` aggregates results per version |
| Verified | `metadata.verify` — validated at the **write boundary** (malformed gates are rejected, never silently no-op'd); enforced at the store's DONE flip, so no executor path can self-grade |
| Self-revising | goal-close auto-writes a plan-vs-actual review; fix-beads carry `metadata.divergence: good\|bad`; good feeds template revision, bad feeds RCA |

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
   first-class: `awaiting_verify` (human gate parked, surfaced to the named person) and
   `verify_failed` (retryable; **neither status releases downstream `blocked_by`**,
   killing the momentarily-done race). First failure spawns one sibling fix-bead;
   second failure escalates to a human — never a third autonomous attempt.

### Operational clauses proven in production

- **Stuck-gate quarantine:** a bead in `awaiting_verify` beyond N days moves to a weekly
  digest — abandonment degrades to silence, not queue noise.
- **Legacy scope guard:** gates apply only where a `verify` payload (or ASOP lineage)
  exists; thousands of pre-contract items keep legacy semantics. No backfill, no flood.
- **Watchdogs at the seam:** silence-based idle timeout and an explicit completion
  marker (`AGENTCO_DONE:`) make "agent went quiet" a handled state, not a discovery.

*Provenance: contract adversarially reviewed (architecture + operations lenses) before
implementation; gate placement moved from executor to store as a result. Prior-art
review 2026-08-31 (AWS Agentic SOPs/Strands, Decagon AOPs, Skan, Agent-S): instruction
documents all — none carry per-version outcomes, embedded gates, or divergence-driven
revision. See `decisions/asop.md`.*
