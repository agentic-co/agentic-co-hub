# 0015 — Harness knowledge write-back: lessons and proposals leave only through the 0006 clean room

**Date:** 2026-11-13 · **Status:** PROPOSED · **Supersedes:** nothing; implements the harness side
of [`0006`](0006-hub-memory-design.md) §3 (residuals authority) and uses the existing proposal path
(`POST /sops/{id}/propose`), memory routes (`/memory`, `/memory/search`, `/memory/traces`) and
[`0009`](0009-ontological-memory.md) spaces

> A decision with no revisit condition is doctrine. Every record here carries one.

## Context

The harness learns two kinds of thing while it executes:

- **Procedural knowledge:** a step that keeps failing the same way, a gate that refuses correct
  work, a missing input. Lesson triage already classifies these locally (fact, defect, proposal,
  noise) and files fix runs. Nothing reaches the hub.
- **Memory knowledge:** facts about the systems it worked on and outcomes it observed.

The hub already decided how such knowledge may leave an owner's partition: 0006 §3 defines a
clean room, with owner-defined categories, a never-leave list no approval overrides, a mandatory
provenance pointer, a blind validator, an aggregate check per owner and batch approval. 0006 §3
also says "the same path applies to ASOPs." What is missing is the harness's half: nothing
produces residuals, and nothing checks them before they are written down.

An internal prototype runner shows the cost of skipping this: it scrubs only secret-shaped
strings, and its server's revise endpoint is never called by the runner. Its procedural learning
stays local.

## Decision

**(c).** The harness writes back, but only as **proposals into 0006's clean room**, never as
direct writes.

1. **No new memory design.** The harness is a client of the existing hub surfaces. It reads with
   `memory search` (clearance enforced by the hub, 0006 §2) and writes only through:
   - `memory submit` → a residual item for the shared layer, with its provenance pointer,
   - `sop propose` → `POST /sops/{id}/propose`, a procedure-change proposal; the hub's revision
     policy and the owner decide, and nothing is ever auto-activated.
2. **Session and episodic memory never leave the harness** (0006 §1). Raw traces go to
   `/memory/traces` only on the owner's own personal hub, never to a company hub.
3. **The mechanical half of the clean room runs in the harness, before the outbox**
   ([`0013`](0013-harness-pull-work-push-events.md) §5). It checks secret shapes, the owner's
   never-leave list and internal-names list, identifiers, hostnames, repository names and people.
   It is **fail-closed**: a missing list refuses the submission rather than passing it. The model
   writes the residual; code judges it.
4. **The lists are private, per-partition configuration.** They are never committed to a public
   repository; public tests plant fabricated names.
5. **The blind validator, the aggregate check and batch approval stay on the hub,** as 0006 §3
   placed them. The harness pre-check reduces what reaches them; it does not replace them.
6. **Write-back is off by default** and enabled per partition by the owner (0013 §6).

## Alternatives

- **(a) Keep lessons local** (today). Procedures never improve across nodes, and the plane's
  proposal path stays unused.
- **(b) Let the harness revise procedures directly** on the hub. That bypasses the revision
  policy and the clean room, and 0006 rules it out.

## Revisit condition

Revisit if the harness pre-check and the hub's blind validator disagree on more than one item in
ten (one of them is miscalibrated), if the owner's batch-approval queue stops being read (0006's
own condition), or if fewer than one proposal a month survives approval across all nodes, which
would mean write-back is not producing usable knowledge.
