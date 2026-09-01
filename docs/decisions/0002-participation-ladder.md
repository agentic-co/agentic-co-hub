# 0002 — Participation is a ladder, and the filesystem is its floor

**Status:** accepted · **Date:** 2026-08-31

> A decision with no revisit condition is doctrine. Every record here carries one.

## Context

The project's stated premise is that Tier 1 "is the only one that reaches a
harness nobody configured." Every verb that matters lives in Tier 2, behind a
config line. Both statements are in the README and they describe different
products.

Read them together and the honest position today is: **read-agnostic for
everyone, write-agnostic for the configured.** A harness that configured nothing
receives a spliced block and can do nothing with it. It is an audience, not a
participant — and every property the ASOP contract is built on (pinning, gating,
divergence) requires a write.

Closing that gap is the difference between a coordination layer and an MCP
server with good primitives.

## Alternatives

**(a) One config line is the price.** Tier 1 stays read-only forever; invest in
making the MCP/HTTP entry trivial and conformance-tested. Simple, honest, and it
concedes the premise for writes.

**(b) The filesystem is the protocol.** Drop MCP to a thin wrapper over a
file-based path. Fewest surfaces; makes best-effort writes the only write path.

**(c) A ladder — outbox floor, MCP fast path.**

## Decision

**(c).** Participation is four levels, and a harness declares its own:

| Level | Capability | Cost to the owner |
|---|---|---|
| **L0 observer** | reads the spliced managed block | zero |
| **L1 publisher** | writes `.agentco/outbox.jsonl`; a local drainer signs and publishes | zero |
| **L2 worker** | MCP or HTTP — pull, report, attest | one config line |
| **L3 verifier** | declares the `verify` capability; runs judged gates | deliberate setup |

Three subordinate decisions follow from it.

**Gate enforcement splits by class.** Part I already nominates different
enforcers and the difference is easy to miss: a `deterministic` gate is "re-run
fresh by the *completing* process", while a `judged` gate needs "a route
different from the executor's". So deterministic gates run on the executor's
machine and submit an **attestation**; judged gates become verify work items
routed to L3 by capability; human gates go to the routing spine. All three carry
a park clock and a declared default.

**The signature decides the actor; the body never does.** The outbox is local
IPC, not a trust boundary: the agent writes an unauthenticated line and the
drainer signs it with the machine's credential. Anything the agent claimed about
its own identity is carried as `agentLabel` and rendered **explicitly
unverified** — useful for reporting, never promoted to an authenticated fact.

**The tool ceiling moves from nine to twelve**, adding `attest`, `sop_revise`
and `sop_activate` as first-class verbs. Twelve is the new stated number, not
the absence of one.

## Reasoning

MCP is not universal. Cursor, Aider, Codex and a bespoke in-house agent each
speak it differently or not at all. **Every coding agent can read and write
files in the repo it is working in** — that is the only substrate common to all
of them, which makes it the only honest floor for a layer claiming harness
agnosticism.

The obvious objection to a file-based write path — anyone with repo write could
forge an actor — does not land, because the file never crosses a machine. The
drainer holds the key. This preserves the invariant the work queue already
enforces for leases: *a worker that can name itself can take another worker's
lease, and the fence would faithfully record the theft as legitimate.*

`attest` was nearly folded into `work_report`, and the argument for folding was
good: an attestation bundled with the completion claim arrives atomically and
can be *required* for a gated item, where a separate call can simply not happen.
That property is kept as a **refusal rule** rather than as a schema shape — a
report on a gated item without an attestation is refused — so first-class verbs
cost nothing in integrity.

## Revisit condition

Two numbers, both published by the plane.

**The ladder is wrong if L1 does not convert.** If publishers who arrive at L1
do not reach L2 within a trailing window, the outbox is not a floor — it is a
terminus, and the config line was never the obstacle. The fix would be (a), not
more outbox features.

*Instrumented 2026-09-01, with L1 itself:* `metrics.l1_conversion`, reported by
`agentco metrics`. It is deliberately a population measure plus an
authenticated lower bound rather than a per-person funnel — the drainer signs
as the machine, so a per-identity join would report zero conversion whether the
ladder worked or not. The falsification threshold is written into the code
ahead of any data: three distinct L1 harnesses over eight weeks with no
authenticated conversion and no L2 growth. Until an L1 publisher is ever seen
the metric reports `null`, never `0` — "nothing to measure" and "measured, and
nobody converted" are opposite findings.

**The ceiling is wrong if schema bytes outgrow the count.** Twelve is a count,
and the README justifies the cap by *context cost*, which is bytes. A byte
budget is published alongside the count; if it grows while the count holds, the
count has stopped measuring the thing it was chosen to measure.

## Consequences

- Two write paths must stay semantically identical. That is a real maintenance
  cost, and the conformance suite is what makes it checkable rather than hoped
  for.
- An outbox write is fire-and-forget. Without a receipt path this produces "I
  pushed and nothing happened", which the project already names as *the most
  adoption-lethal outcome available*. Receipts are therefore not optional
  polish; they are part of L1.
- A deterministic attestation is only as honest as the process that submits it.
  A sloppy or compromised executor can report exit 0 on a check it never ran.
  L3 closes that; attestation alone does not, and the docs must not imply
  otherwise.
- `divergence` now names two unrelated concepts — snapshot pointer movement and
  ASOP plan-vs-actual. One of them has to be renamed before both ship.
