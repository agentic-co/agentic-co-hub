# 0015 — Harness knowledge write-back: lessons and proposals leave only through the 0006 clean room

**Date:** 2026-11-13 · **Status:** PROPOSED · **Supersedes:** nothing; implements the harness side
of [`0006`](0006-hub-memory-design.md) §3 (residuals authority). Procedure changes travel as
`sop_revise` (`POST /sops/{id}/revise`); memory reads use `/memory/search`; the memory write path
is a gap (see "What the hub lacks today"). Uses [`0009`](0009-ontological-memory.md) spaces

> A decision with no revisit condition is doctrine. Every record here carries one.

## Context

The harness learns two kinds of thing while it executes:

- **Procedural knowledge:** a step that keeps failing the same way, a gate that refuses correct
  work, a missing input. Lesson triage already classifies these locally (fact, defect, proposal,
  noise) and files fix runs. Nothing reaches the hub.
- **Memory knowledge:** facts about the systems it worked on and outcomes it observed.

The hub already decided how such knowledge may leave an owner's partition: 0006 §3 defines a
clean room, with owner-defined categories, a never-leave list no approval overrides, a provenance
pointer that is "a hard field, not a comment", a blind validator, an aggregate check per owner and
batch approval. 0006 §3 also says "the same path applies to ASOPs." What is missing is the
harness's half: nothing produces residuals, and nothing checks them before they are written down.
The hub's half is also only partly built (see "What the hub lacks today").

An internal prototype runner shows the cost of skipping this: it scrubs only secret-shaped
strings, and its server's revise endpoint is never called by the runner. Its procedural learning
stays local.

## Decision

**(c).** The harness writes back, but only as **proposals into 0006's clean room**, never as
direct writes.

1. **No new memory design.** The harness is a client of the hub surfaces. It reads with
   `GET /memory/search` (clearance enforced by the hub, 0006 §2) and writes only through:
   - a **memory residual** for the shared layer, with its provenance pointer. The hub has no verb
     for this yet (see "What the hub lacks today").
   - **`sop_revise`**: `POST /sops/{id}/revise`, outbox verb `sop_revise`. It creates the next
     version of the procedure as a DRAFT, authored by the signing credential. The hub's revision
     policy (`agentco/policy.py`, shared with the spec's `asop.revision`) applies to that author as
     to any agent.
   - **The harness never sends `sop_activate`.** Activation is the owner's act. This is a rule on
     the harness, not a guarantee the hub gives: `POST /sops/{id}/activate` accepts an agent-signed
     call when the revision policy allows it, so an operator who wants the guarantee on the hub
     limits the harness credential's `routes` (`agentco/authz.py`) to leave `sop_activate` out.
   `POST /sops/{id}/propose` is not this path. It drafts the next version from adjudications
   already pending on the hub and takes no changes in its body.
2. **Session and episodic memory never leave the harness** (0006 §1). `POST /memory/traces` is not
   a place for them: it carries which memory items a finished work item invoked, so the hub can
   credit them against a verified outcome (`docs/memory.md`). The harness reports that, when
   memory is on, and nothing else about a run.
3. **The mechanical half of the clean room runs in the harness, before the outbox**
   ([`0013`](0013-harness-pull-work-push-events.md) §5). It checks secret shapes, the owner's
   never-leave list and internal-names list, identifiers, hostnames, repository names and people.
   It is **fail-closed**: a missing list refuses the submission rather than passing it. The model
   writes the residual; code judges it.
4. **The lists are private, per-partition configuration.** They are never committed to a public
   repository; public tests plant fabricated names.
5. **The blind validator, the aggregate check and batch approval belong on the hub,** as 0006 §3
   placed them, and are not built there yet. The harness pre-check reduces what reaches them; it
   does not replace them.
6. **Write-back is off by default** and enabled per partition by the owner (0013 §6).

## What the hub lacks today

Memory is experimental and off unless `AGENTCO_MEMORY=on`; every `/memory` route answers
`404 memory_disabled` otherwise (`docs/memory.md`).

1. **No memory verb in the outbox.** `PUSH_VERBS` has none (`agentco/outbox.py`), and the MCP
   server has no memory tool; the CLI has no `memory submit`. Until the outbox has a memory verb,
   only the procedure half of this record is buildable.
2. **No residuals path on the hub.** `POST /memory` writes an authoritative item directly, at a
   label the caller is cleared for. It requires `provenance` as `{kind, ref}`, but nothing holds a
   proposed item for review: the blind validator, the per-owner aggregate check, the owner's batch
   approval and category graduation are the residuals pipeline that `docs/memory.md` lists under
   "Not built yet". Decision 5 describes a hub half that does not exist, and the memory half must
   not ship until it does.
3. **No provenance field on a procedure version.** 0006 §3 requires a provenance pointer on a
   residual. `sop_revise` does not accept one: the body keys are `title`, `idempotencyKey` and the
   record's own fields (`SOP_BODY_KEYS`, `agentco/app.py`), the route is strict, and an unknown key
   such as `provenance` is refused `422 unknown_field`. The stored version records `author` and
   `author_kind`, and free-text `proposals` per procedure and per step, and nothing else about
   where the change came from. A procedure change from the harness therefore carries no
   machine-checkable pointer today. Either the spec's record gains the field or the pointer lives
   in the harness journal until it does.
4. **No stale-version refusal on `sop_revise`.** It always writes latest + 1; there is no
   `expected_version`. Two harnesses revising the same procedure produce two consecutive drafts,
   not a conflict.

## Alternatives

- **(a) Keep lessons local** (today). Procedures never improve across nodes, and the plane's
  proposal path stays unused.
- **(b) Let the harness activate its own revisions.** That bypasses the owner's decision on what
  becomes the default, and the clean room, which 0006 §3 says applies to ASOPs too.

## Revisit condition

Revisit if the harness pre-check and the hub's blind validator disagree on more than one item in
ten (one of them is miscalibrated), if the owner's batch-approval queue stops being read (0006's
own condition), or if fewer than one proposal a month survives approval across all nodes, which
would mean write-back is not producing usable knowledge.
