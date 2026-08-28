# AgentCo

**A coordination layer for organisations running more than one agentic harness.**

Everyone on your team is running their own AI coding agent. Different tools, different
vendors, no two configured the same — and nobody is giving theirs up. Individually they
work fine. Together they are blind to each other:

- Two agents edit the same directory and nobody finds out until the merge.
- One builds against a spec that changed last Tuesday.
- An agent hits something it cannot decide, asks a human, and the question goes nowhere —
  because nothing guaranteed it was delivered.

AgentCo holds the three things nothing else holds: **the claims people and agents make
about each other's work**, **the pointers you built against, so you are told when they
move**, and **a router that puts a decision in front of a named human and records the
acknowledgement**.

## What it is not

**It is not your system of record.** Jira, Azure DevOps, Linear and GitHub Issues stay
authoritative. AgentCo never becomes the place work lives, and it holds no competing
version of a fact another system owns.

**It never blocks anyone.** Scope claims are advisory. If AgentCo is down, every tool
falls back to exactly what it does today.

**It does not replace your harness.** Claude Code, Cursor, Copilot, Aider, a bespoke
in-house agent — keep all of them. Integration is one config entry, and nothing about
how your harness works changes.

## The bet

Across nine company engineering blogs, Stripe's and ThoughtWorks' published practice, and
the whole agent-operating-system literature, we could not find anything addressing
coordination **across independently-owned agent harnesses**. Plenty on orchestrating
agents you control. Nothing on coordinating agents you don't.

That is either the thesis or the warning, and we could not tell which from the desk.
Publishing is the experiment: if nobody else has this problem, nobody uses this, and that
is a real answer for the cost of a repository.

## Status

**Early.** The coordination primitives are implemented and tested; the surface is small
on purpose. See [`docs/`](docs/) for the design, and
[`docs/roadmap.md`](docs/roadmap.md) for what is built versus what is planned.

The project's own adoption gate is written down and deliberately hard to game: **two
identities other than the author publishing weekly for four consecutive weeks.** Stars
are the vanity metric that will be available; weekly publishers are not. A public repo
with no users is a worse outcome than a private tool with two, because it looks like
adoption while being none.

## Design principles

These are load-bearing, and each was derived from a failure mode rather than from taste.

| Principle | Why |
|---|---|
| **Any tool may push. No tool may file.** | A push is a signal; the owning controller decides what becomes work — with a clock, because "I pushed and nothing happened" is the most adoption-lethal outcome available. |
| **Advisory, never blocking.** | Making concurrency visible is most of the value and none of the political cost — but only if it never stops anyone working. |
| **Pointers, never copies.** | Snapshots record a URI plus a cheap version token. No document body is ever stored. A second document store is how a coordination layer becomes a system of record by accident. |
| **Fail static.** | If the plane is down, nothing is blocked; it reconciles when it returns. |
| **Every refusal carries a remediation.** | A tool that refuses without saying what to do instead teaches people it is broken. |
| **Anything naming a company is configuration, not code.** | Which is what makes this repo publishable at all — see [CONTRIBUTING](CONTRIBUTING.md). |

## Getting started

Not yet — the extraction from the original private implementation is in progress. Watch
[`docs/roadmap.md`](docs/roadmap.md).

## Licence

[Apache License 2.0](LICENSE). Apache rather than MIT for the explicit patent grant,
which matters when the thing being adopted is infrastructure inside a company.
