# ASOP — Agentic Standard Operating Procedure (term definition)

**Date:** 2026-08-31 · **Status:** adopted

## The term

An **ASOP** is an SOP an agent can execute *and be held to*. The industry ships "agent
SOPs" as instruction documents (AWS Agentic SOPs / Strands, Decagon AOPs, Skan AOPs) —
prose an agent reads. An ASOP is defined by three properties the document form lacks:

1. **Versioned** — every revision is a distinct version; outcomes are recorded
   *per version* (`outcomes_by_version`), so "did the change help" is a count, not an
   opinion.
2. **Verified** — the procedure carries its own definition of done: a deterministic
   check, a judged rubric fixed at authoring time, or a named human's acknowledgement.
   A run that cannot pass its gate did not complete, whatever the transcript says.
3. **Self-revising** — divergence between the procedure and what execution actually did
   is captured (tagged good/bad) and feeds the next version. Good divergence revises the
   ASOP; bad divergence routes to root-cause.

One line: *an SOP tells an agent what to do; an ASOP can prove it was done and gets
better when it wasn't.*

## Why we coin it

AgentCo's SOP objects already implement all three properties. Naming the contract —
rather than the document — is the point: it is the difference between prompt files and
operating procedure as infrastructure. Prior art reviewed 2026-08-31 (AWS, Decagon,
Skan, Agent-S paper): the acronym is unclaimed; the contract is unmatched.
