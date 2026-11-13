# 0016 — Procedures are pinned by digest, and a run may finish on the version it started

**Date:** 2026-11-13 · **Status:** PROPOSED · **Supersedes:** nothing; extends the procedure
library's versioning ([`0003`](0003-asop-v3-adoption.md)) on both the hub and the harness

> A decision with no revisit condition is doctrine. Every record here carries one.

## Context

Both products version procedures: the hub in its library (version, supersession, retirement) and
the harness in `asops.jsonl`. Neither pins a version's **content**:

- A version is identified by number, not by a digest of its document. Two stores can hold a
  "version 3" with different bytes and nothing notices.
- When a new version is activated, a run filed under the old one has no stated right to finish
  on it. A chain that spans hours can see its later steps resolved against a document its earlier
  steps never saw.

An internal prototype runner pins every procedure file by sha256 in a library manifest and
refuses a mismatch. A version also declares which earlier versions it is `compatible_with`, so an
open run may continue on its original version after an upgrade.

## Decision

**(b).**

1. **Every procedure version carries a content digest** computed with the spec's normative
   canonical JSON (`asop.manifest.canonical_json`, spec §3.10, the function 0008 uses for the answer envelope). A run's plan records the
   digest, not only the version number.
2. **Mismatch is refused.** A harness whose local copy of a pinned version has a different digest
   refuses to execute it, with a refusal code, and re-fetches from the authoritative store.
3. **Runs finish on the version they started,** unless the new version declares
   `compatible_with` the old one *and* the owner opts the run in. Activation never silently
   migrates an open run.
4. **The digest is additive.** A store that does not send one behaves as it does today; adoption
   into the spec follows the 0008 path.

## What the hub lacks today

The hub's procedure record has no digest: it carries `version`, `status`, `superseded_by`,
`author` and `author_kind` and the content fields (`asop.sop.ASOP`), and nothing in `agentco/sop.py`
computes one over a version. A run is filed against a version number. Decision 1 is new work on the
record and on the run's plan, after the spec change.

## Alternatives

- **(a) Version numbers only** (today). Byte drift between stores is undetectable.
- **(c) Always migrate open runs to the newest version.** That is simpler, but it breaks the
  evidence chain, because a step's gate result was judged against a document the run no longer
  points at.

## Revisit condition

Revisit if digest mismatches are never observed across a quarter of multi-node use (the check
would be cost without signal), or if `compatible_with` declarations are routinely wrong, which
would mean compatibility needs to be tested rather than declared.
