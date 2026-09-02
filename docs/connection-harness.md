# The connection harness — implementation plan

> This is the internal build plan. The user-facing "how do I connect my harness"
> guide is [`participation.md`](participation.md) — a near-identical filename was
> the first thing a reader tripped over, so the two are named apart deliberately.

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
| Decomposition bounds — parent/child, review bound, depth, repair beside | built (`work.enforce_decomposition`) | — |
| Tier-1 splice (`inject`), session hook | built | L0 |
| MCP surface, HTTP surface, HMAC | built | L2 |
| Versioned SOPs, `outcomes_by_version`, drift | built | — |
| `verify` payload, gate statuses, attestation | built — on all three transports | L2/L3 |
| **Outbox + drainer + receipts** | built ([`docs/outbox.md`](outbox.md)) | **L1** |
| `verify` capability, judged routing, park clocks, quarantine digest | built (`agentco verifiers --route --sweep --quarantine`) | L3 |
| Write-back to an external system of record | built, opt-in ([`docs/writeback.md`](writeback.md)) | — |
| **Revision policy** — three rules an agent revision obeys, on `revise` and `activate` | built ([`agentco/policy.py`](../agentco/policy.py)) | — |
| **Adjudication tagging** — `good`/`bad`, adjudicator ≠ executor | built (`Queue.adjudicate`) | — |
| **Plan-vs-actual** — plan under the pin, review at completion | built (`work.plan_vs_actual`) | — |
| **Revision proposals** | **absent** | — |
| **`sop_revise` / `sop_activate` on MCP** | HTTP only; reserved in the outbox push set | L2 |
| Conformance suite | absent | all |

One of those absences is worth naming plainly. **Shared learning, the headline
claim, still has no write path on the primary surface**: a harness on MCP can
read a procedure but cannot contribute a lesson to it. The one write path that
does exist, `revise` over HTTP, is now policed (P4.0 — the first unit of Phase
4, built); the loop that would feed it is not. That is the rest of Phase 4, and
it and Phase 5 (conformance) are what remains of the phases below — Phases 0
through 3 are built.

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
The order below is deliberate: the policy comes first, because the write path
it governs already exists over HTTP, and the first real consumer of this phase —
a harness running defect, feature and data-fix procedures against production
systems — needs the rails before it needs the loop.

- **Revision policy** — built. The rules `revise` and `activate` enforce when
  the reviser is an agent ([`agentco/policy.py`](../agentco/policy.py)). Generic
  to every registry, and each one computable because versions are immutable and
  each records its author and whether the operator had declared them human:
  1. **Protected tags freeze a step against agents.** A version carrying a
     protected tag — `money` and `irreversible` ship as the defaults; a registry
     adds its own through `AGENTCO_PROTECTED_TAGS` and can never remove those
     two — cannot be revised or activated by an agent at all, and no agent
     revision adds or removes a protected tag on any step. Tags are
     case-folded, so `Money` is not a way past a rule written for `money`.
  2. **A step's class ratchets toward human only.** `executor: human | agent`
     is the class. An agent may make an agent step human; it may never make a
     human step an agent step, or an unclassified one, which is the same
     demotion with the label removed.
  3. **No undoing a human.** An agent revision may not move any field into a
     state a human moved it away from — bring back what a human removed, remove
     what a human added, or restore what a human replaced — until a human moves
     it back. Versions written before authorship was recorded impose nothing.
  **Who is human is declared, never inferred**: the operator names human actors
  (`AGENTCO_HUMANS`), everyone else is an agent, and an undeclared registry
  polices every reviser. The kind comes from the authenticated actor and the
  declaration, never from the body. A human reviser is bound by none of the
  rules; what they protect — which steps exist, which are human, which carry
  `money` — is registry content that never lives in the plane. `activate` is
  policed against the active version, or the rule an agent cannot break by
  revising it breaks by re-activating the version from before the human's
  change. A refusal is HTTP 403 `revision_policy:<rule>`, and it writes
  nothing. The class and the tags are load-bearing, not labels: `instantiate`
  refuses an instance of a human or protected step that does not carry a
  `human` gate, on every transport.
- **Adjudication tagging** — built. `metadata.adjudication` on a work item:
  `good` / `bad`, the adjudicator's identity, pointed evidence, the executors
  it was checked against, and the pinned `sop_ref` so P4.3 can route it to the
  procedure. **Adjudicator ≠ executor, enforced, not documented**: the
  adjudicator is the authenticated actor on every transport, compared against
  every identity the plane recorded as having executed the item — the lease
  holder, the holder who reported it, and on a deterministic gate the
  attester — none of which a caller can set. An unexecuted item has no
  divergence to judge and is refused; a second adjudication is a dispute and is
  refused rather than overwritten (ASOP v2 routes disputes to escalation). The
  key is reserved at create, so it cannot be forged into an item. Transports:
  `POST /work/{id}/adjudicate`; an `adjudication` rider on `attest` over HTTP,
  MCP and the outbox — which is how it reaches the primary surface without a
  thirteenth tool; and the `adjudicate` outbox verb. Refusals write nothing;
  a rider the executor offers refuses the whole `attest` call.
- **Plan-vs-actual review** — built. `instantiate` copies the procedure's own
  words (`title`, `definition_of_done`, `validation`, `entry_check`) under the
  pin as `metadata.sop_plan`, so the review reads what the executor was handed
  even after the procedure moves on. `report_result` on an item that pins a
  procedure writes `metadata.plan_vs_actual` at the moment of completion, while
  the lease still names the executor: plan beside actual — reported status,
  landed status, result, attempt, attestation, filed and reported times — plus
  computed flags (`gate_disagreed`, `retried`, `failed`, `awaiting_verdict`)
  that say where to look first. A verifier's later verdict lands beside the
  executor's claim, never over it. Nothing is judged: the plane records, the
  adjudicator concludes, and this is what they read. Both keys are reserved at
  create; `instantiate` holds the caller's own metadata to that rule before it
  files. An item that pins no procedure gets no review.
- **Revision proposals** accumulating against the template — good adjudications
  feed the next version, bad ones feed root-cause. Proposals pass through the
  same policy as any other agent revision.
- **`sop_revise` and `sop_activate` on MCP**, closing the shared-learning write
  gap — behind the policy, or the gap closes onto an unpoliced path.

*Proven by:* the eval harness in [`../evals/`](../evals/README.md), which
already exists. Phase 4 is the first point at which its `asop_lesson` arm has
something real to measure — until the loop closes, the lesson channel is
hand-fed. The policy was proven by mutation, the Phase 1 way: each rule, the
lift, the activate path, the instantiate gate and the fail-closed default were
removed in turn and a test failed each time ([`tests/test_revision_policy.py`](../tests/test_revision_policy.py)).

**Beside this phase, not inside it: decomposition bounds enforced at create —
built.** `metadata.parent` makes an item a child; `metadata.repairs` makes it a
repair. A parent holds at most seven children — six work units and the verify
unit that closes them (`AGENTCO_MAX_CHILDREN` raises it; the contract's escape
hatch, explicit) — and a tree goes at most three deep (`AGENTCO_MAX_DEPTH`),
which is 343 leaves. A child is written into its parent's `blocked_by` in the
same lock that files it, so the parent cannot close early. A child of a
missing or closed parent is refused. A repair goes beside the unit it repairs
— under the same parent or none, never beneath it — consumes no review budget
and blocks nobody, because the red original it repairs already does. The
contract states the bound ([`asop.md`](asop.md) § Decomposition bounds) and
readers keep taking it for a cap on total work rather than the human review
bound it is — recursion is how the bound is honoured. Refusals are HTTP 422
`decomposition_bound` and write nothing (`work.enforce_decomposition`,
[`tests/test_decomposition.py`](../tests/test_decomposition.py)).

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

Rollback and compensation (ASOP v2 excludes it explicitly), and dispute
arbitration beyond escalation. Writing to a system of record has exactly one
opt-in, notice-only exception — [`docs/writeback.md`](writeback.md) — off
until configured, and even configured it can only tell a record its gate is
waiting, never change a state or close a ticket. Outside that one path,
writing to a system of record stays off the table, because the whole value
proposition is that adopting AgentCo cannot damage the system you already
trust.
