# 0013 — The harness pulls work and pushes everything else through the outbox

**Date:** 2026-11-13 · **Status:** PROPOSED · **Supersedes:** nothing; places the harness on two
rungs of [`0002`](0002-participation-ladder.md) at once (L1 publisher and L2 worker)

> A decision with no revisit condition is doctrine. Every record here carries one.

## Context

Today the harness is an L2 worker with **publishing off by default**: it pulls, reports and
attests over HTTP, and anything else it learns (a lesson, a proposal, a status change on a
standalone bead the owner wants visible) has no path to the plane. When the hub is down, a report
is retried in-process and lost if the process dies.

The hub already has most of this floor. The L1 outbox (`docs/outbox.md`,
`agentco/outbox.py`) is one JSON line per call (`line_id`, `at`, `verb`, `payload`,
`agent_label`), drained and signed by a local process holding the machine credential, with
`line_id` as the idempotency key for the verbs that publish through the registry. An internal prototype runner uses the same shape
(content-keyed, idempotent, version-conflict aware) and survives hub outages because the queue is
on disk.

## Decision

**(c).** Work moves by pull; everything the harness *says* moves by push.

1. **Work stays pull.** `pull`, `renew`, fenced leases and attempt ids are unchanged (0002 L2).
2. **Every outbound statement goes through the outbox:** reports, attestations, lifecycle edges
   ([`0012`](0012-harness-authority-and-journal.md) §3), procedure revisions and, once the hub has
   a verb for them, memory submissions ([`0015`](0015-harness-knowledge-write-back.md)). The
   harness writes the line; the drainer signs and publishes it. The signature decides the actor,
   never the body (0002).
3. **Idempotent by construction.** The writer supplies a `line_id` derived from content plus
   attempt (not random), so a replay after a crash is a no-op on the hub. This is a rule on the
   writer. `Outbox.push` (`agentco/outbox.py`) takes an optional `line_id` and, when none is given,
   generates a random `ob_<uuid>` one, so a writer that calls `push()` without one gets a fresh
   key on every call and no replay protection.
4. **Version conflicts are explicit.** A push that targets a stale version is refused with a
   conflict code, and the drainer re-reads before retrying. It never overwrites.
5. **Sanitize before the line is written,** not at drain time
   ([`0015`](0015-harness-knowledge-write-back.md) §3). The outbox file is local IPC, but it is
   also a durable record on disk, and content that must not leave should never be written to it.
6. **Publishing stays opt-in per verb class.** Work reports are on by default once a hub is
   configured; memory and proposal verbs are off until the owner enables them.

## What the hub lacks today

- **No memory verb.** `PUSH_VERBS` holds `claim_scope`, `release_scope`, `snapshot`,
  `work_report`, `attest`, `adjudicate`, `sop_revise` and `sop_activate`; `PENDING_VERBS` is empty.
  A memory line is refused `verb must be one of [...]`. Until a verb is added to `PUSH_VERBS` and
  mapped in the publisher (`agentco/outbox.py`, the verb dispatch near `sop_revise`), memory
  cannot ride the outbox.
- **No derived default `line_id`.** A helper that derives one from content and attempt is a change
  to `Outbox.push` or a harness-side function. It does not exist in the hub today.
- **No conflict code on revise.** `sop_revise` creates the next version of the latest one and has
  no `expected_version`, so decision 4 has no hub-side stale-version refusal to rely on for
  procedures. Memory revise does take `expected_version` (`docs/memory.md`).

## Alternatives

- **(a) HTTP calls with in-process retry** (today). These are lost on crash, and every new verb
  needs new retry code.
- **(b) Push everything, including work.** That needs push delivery to workers, which the plane
  does not have and does not need (pull is what makes leases fenceable).

## Revisit condition

Revisit if drain latency ever makes a lease expire before its report lands (that would argue for
a direct HTTP fast path for reports), or if outbox files grow without bound on any node for a
week, which would mean the drainer is not a reliable part of the install.
