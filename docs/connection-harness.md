# The connection harness — implementation plan

> **Goal:** a coordination plane any agentic harness can connect to, without the
> plane knowing or caring which one it is.

Design decisions and their revisit conditions:
[`decisions/0002-participation-ladder.md`](decisions/0002-participation-ladder.md).
The contract being implemented: [`asop.md`](asop.md).

---

## Where the work actually is

The primitives are built. What is missing is almost entirely the **participation
edge** — the parts that let a harness the plane has never met do more than read.

| Capability | State | Level it unlocks |
|---|---|---|
| Scope claims, snapshots, change feed, work queue, leases | built, some proven | L2 |
| Tier-1 splice (`inject`), session hook | built | L0 |
| MCP surface, HTTP surface, HMAC | built | L2 |
| Versioned SOPs, `outcomes_by_version`, drift | built | — |
| **`verify` payload, gate statuses, attestation** | **absent** | L2/L3 |
| **Outbox + drainer + receipts** | **absent** | **L1** |
| **`verify` capability, judged routing, park clocks** | **absent** | **L3** |
| **Adjudication tagging, plan-vs-actual, revision proposals** | **absent** | — |
| **`sop_revise` / `sop_activate` on MCP** | HTTP only | L2 |
| Conformance suite | absent | all |

Two of those absences are worth naming plainly. **Nothing in `agentco/`
implements gates at all** — Parts II and III of the ASOP spec describe a runtime
that lives elsewhere. And **shared learning, the headline claim, has no write
path on the primary surface**: a harness on MCP can read a procedure but cannot
contribute a lesson to it.

---

## Phase 0 — Clear the ground

Small, no new concepts, and everything downstream is cheaper afterwards.

**Resolve the `divergence` collision.** The word names two unrelated things:
snapshot pointer movement (`agentco/divergence.py`, shipped and documented) and
ASOP plan-vs-actual. The spec already supplies the better word for the second —
it calls the tag *"an adjudication, not a confession"*. ASOP divergence becomes
**adjudication**; snapshot divergence keeps its name and its docs.

**Thread `agentLabel`.** An unverified, self-reported harness name carried
beside every authenticated actor, rendered with an explicit marker wherever it
surfaces. One field, one rendering rule, touched once now rather than in five
places later.

*Proven by:* a test that `agentLabel` never appears in any position where the
authenticated actor is expected, and that a payload attempting to set `actor`
is refused on every transport including the outbox.

---

## Phase 1 — Gates

The `verified` property, which everything else in the contract rests on.

- **`verify` payload on work items**, validated at the *write boundary*. A
  malformed gate is rejected, never silently no-op'd — a gate that quietly does
  nothing is worse than no gate, because it reports green.
- **Two statuses**: `awaiting_verify` and `verify_failed`. **Neither releases
  downstream `blocked_by`.** This is the momentarily-done race, and it lands
  directly on `WorkItem.unmet_blockers` and derived blockedness — the trickiest
  integration in the phase.
- **Attestation record**: check identity, exit status, environment fingerprint,
  timestamp, submitter identity. The plane verifies the record's *shape* and
  stores the claim. It never runs the command.
- **`attest` tool** (MCP) and endpoint (HTTP).
- **Refusal rule**: a report on a gated item *without* an attestation is
  refused. This is what keeps first-class verbs from costing integrity — the
  atomicity that bundling would have given is bought back as a refusal.
- **Re-verify invariant**: a fix item never substitutes for the work it repairs.
  The failed unit keeps its failed status, still blocking everything downstream,
  until **its own** gate re-runs and passes.
- **Retry policy**: first failure spawns one sibling fix item; second escalates
  to a human; never a third autonomous attempt.
- **Legacy scope guard**: gates apply only where a `verify` payload exists.
  Existing items keep legacy semantics. No backfill, no flood.

*Proven by:* mutation. Remove the "neither status releases blockers" clause and
the momentarily-done test must fail. Remove the write-boundary validation and a
malformed gate must reach storage. A test that cannot fail when the mechanism is
removed proves nothing, and this phase is where that bar matters most.

---

## Phase 2 — The outbox (L1)

The universal write path, and the phase with the most novel failure modes.

- **Format**: `.agentco/outbox.jsonl`, append-only, one verb per line, restricted
  to the *push* set. "Any tool may push. No tool may file" is a rule about the
  outbox before it is a rule about anything else.
- **`agentco drain`**: validates each line, signs with the machine credential,
  publishes, watermarks what it processed. Deployment guidance for launchd and
  systemd, because a drainer nobody scheduled is a file nobody reads.
- **Idempotency** via a client-generated line id, deduped through the existing
  ingest uniqueness rule. A drainer that runs twice must not double-publish.
- **Quarantine, not silence**, for malformed lines — the same shape
  `SopLibrary._write_all` already uses. An agent that writes garbage must find
  out.
- **Receipts** — `.agentco/receipts.jsonl`, written by the drainer, surfaced in
  the next Tier-1 splice along with rejections. This is **not optional polish**:
  an outbox write is fire-and-forget, and without a receipt the L1 experience is
  exactly *"I pushed and nothing happened"*, which the project names as the most
  adoption-lethal outcome available.
- **Single-drainer enforcement** via lockfile, and bounded file growth via
  watermark truncation.
- **Discovery**: the Tier-1 managed block gains a *how to publish* section. This
  is the whole mechanism by which an unconfigured agent learns the outbox exists
   — L1 without it is a feature nobody can find.

*Proven by:* a real second OS process writing the outbox concurrently with a
drain; a kill -9 mid-write leaving a file the drainer recovers from with exactly
one line quarantined; two drainers racing and publishing each line once.

---

## Phase 3 — The L3 verifier

Mostly assembly — capability routing already exists and is tested.

- **`verify` capability**; judged gates become verify work items, invisible to
  workers without it.
- **Liveness**: every gate declares maximum park time, escalation path, and a
  default. Silence past the deadline resolves by the declared default.
- **No verifier configured** is a first-class state: gated work resolves by
  default on the clock rather than parking forever. An org that never sets up
  L3 must still be able to use gates.
- **Stuck-gate quarantine** to a periodic digest.

---

## Phase 4 — Self-revision

The third ASOP property, and the one with a real behavioural claim behind it.

- **Adjudication tagging** (`good` / `bad`) with the adjudicator's identity and
  pointed evidence. **Adjudicator ≠ executor**, enforced, not documented.
- **Plan-vs-actual review** generated at the moment of completion, while the
  context still exists.
- **Revision proposals** accumulating against the template — good adjudications
  feed the next version, bad ones feed root-cause.
- **`sop_revise` and `sop_activate` on MCP**, closing the shared-learning write
  gap.

*Proven by:* the eval harness in [`../evals/`](../evals/README.md), which
already exists. Phase 4 is the first point at which its `asop_lesson` arm has
something real to measure — until the loop closes, the lesson channel is
hand-fed.

---

## Phase 5 — Conformance

The suite is what makes "two write paths" a maintenance cost rather than a
divergence risk.

- **One semantic core, three transports** — outbox, MCP, HTTP — proven to
  produce identical results for identical input. Any behaviour reachable one way
  and not another is a bug the suite names.
- **Per-level conformance tests** a harness owner can run themselves:
  `agentco conform --level L2` exits non-zero with what is missing.
- **Published byte budget** alongside the twelve-tool count, per the ADR's
  second revisit condition.

---

## Risks

**The two-path divergence.** Outbox and MCP will drift, because they always do.
Phase 5 is the mitigation and it is deliberately not last-if-there-is-time.

**Status explosion in the queue.** `awaiting_verify` and `verify_failed`
interact with derived blockedness, leases, fencing and the reaper — four
mechanisms the twelve-process proof already covers less of than it did when
written. Phase 1 should extend that proof, not just add tests beside it.

**L1 may not convert.** The ladder assumes the config line is the obstacle. If
publishers arrive at L1 and never reach L2, the outbox is a terminus and the
premise was wrong. That is the ADR's first revisit condition and it needs the
metric published from the day L1 ships, not retrofitted once the question comes
up.

**Attestation is a trust floor, not a proof.** A sloppy executor can report exit
0 on a check it never ran. Every document that describes attestation must say
so; the moment one of them implies otherwise, the contract is overselling.

---

## Not in scope

Rollback and compensation (ASOP v2 excludes it explicitly), dispute arbitration
beyond escalation, and writing to a system of record — which stays off the table
entirely, because the whole value proposition is that adopting AgentCo cannot
damage the system you already trust.
