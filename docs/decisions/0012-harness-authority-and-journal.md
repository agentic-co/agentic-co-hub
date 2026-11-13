# 0012 — Who owns a bead: the harness is authoritative only for its own work

**Date:** 2026-11-13 · **Status:** PROPOSED · **Supersedes:** nothing; adopts the "Connected" section
of the harness's draft `EMBEDDED_RUNTIME_PLAN.md` as a decision, and applies
[`0008`](0008-control-loop-personal-and-company-hubs.md) §1 (one partition per company) to the harness

> A decision with no revisit condition is doctrine. Every record here carries one.

## Context

A harness keeps a local bead store (`tasks.jsonl`). When it is also an L2 worker
([`0002`](0002-participation-ladder.md)) it mirrors hub work items into that same store as
ordinary beads. Two stores then hold the "same" item, and nothing says which one is right:

- A mirrored bead can reach a terminal state locally (completion hooks fire, local blockers
  release) while the hub still shows the item leased, parked or failed.
- A lost report or an expired lease leaves the local bead DONE and the hub item open. The next
  `pull` can hand the same work to another worker, and both completions look legitimate.
- The harness's sync path today reports DONE/FAILED only; AWAITING_VERIFY and VERIFY_FAILED never
  reach the hub, so a parked gate is invisible to the plane.

An internal prototype runner that this design borrows from avoids the problem by holding no
work items at all: the server owns the queue, and the runner keeps a ledger of what it did.

## Decision

**(b).** Authority follows where the work was filed, and a harness never holds a second
authoritative copy.

1. **Standalone work** (filed on the harness, never offered to a hub): the harness store is the
   only authority. Nothing changes for a solo operator.
2. **Hub-owned work** (pulled from a hub): the hub is the only authority. The harness keeps an
   **execution journal** entry per attempt, holding the attempt id, lease token, artefact
   references, gate evidence and delivery receipts. A journal entry is not a bead: it cannot fire
   completion hooks, release blockers or reach a terminal state locally.
3. **Report every lifecycle edge, not only the end.** Execution-finished and gate-passed are
   separate reports, and AWAITING_VERIFY and VERIFY_FAILED are reported like DONE and FAILED.
4. **Authority does not move mid-run.** A hub outage does not make hub-owned work local. Evidence
   is kept, delivery is retried, and an uncertain external side effect is reconciled before any
   retry. Moving a run between authorities is a later, explicit feature.
5. **Partitions per company (0008 §1).** A harness serving several data owners keeps one store,
   one journal and one hub connection per owner. A bead never carries content across partitions.

## Alternatives

- **(a) Keep mirroring hub items as beads** and reconcile on sync. This is the current behaviour,
  and it is the double-authority bug above.
- **(c) Make the hub the authority for everything,** including solo work. That breaks "runs
  alone" in the harness README and forces a server on every operator.

## Consequences

- The harness grows a journal type separate from `Task`. Existing completion hooks keep firing
  for standalone beads only.
- `doctor` gains a check: no hub-owned item may exist as a terminal local bead.
- The hub needs nothing new; it already fences by attempt.

## Revisit condition

Revisit if a journal entry ever needs to release a local blocker to make progress (that would
mean hub work and local work are really one workflow), or if operators keep running hub work with
the hub unreachable for longer than a lease, which would argue for explicit authority transfer.
