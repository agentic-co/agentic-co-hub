# 0004 — The MCP surface under ASOP v3

**Date:** 2026-09-04 · **Status:** NOT TAKEN — recommendation C noted, the
surface stays at twelve for now (mabidoli, 2026-09-04). HTTP is what the
runtime uses for P3; revisit when an MCP-only harness needs one of these
verbs. · **Relates to:** [`0002-participation-ladder.md`](0002-participation-ladder.md)
(the ceiling), [`0003-asop-v3-adoption.md`](0003-asop-v3-adoption.md)

## The problem

ASOP v3 puts seven verbs on the wire that the MCP surface does not carry:
`sop_retire`, `run`, `run_get`, `run_list`, `promote`, `adjudicate`,
`outcomes`. All seven are implemented and conformed over HTTP. None is
reachable from a harness that speaks only MCP.

The ceiling is twelve tools and twelve are registered: `claim_scope`,
`release_scope`, `snapshot`, `work_pull`, `work_report`, `work_create`,
`sop_get`, `sop_revise`, `sop_activate`, `attest`, `events`, `whoami`. So the
gap is not an oversight to close by adding tools. It is a decision that has to
be taken deliberately, because the ceiling was itself a deliberate decision
with a published revisit condition.

**What the gap actually costs.** An MCP-only harness today can execute a run
someone else filed — it pulls a step bead, reads the procedure it is pinned to
with `sop_get`, reports, and attests. What it cannot do is *start* one. That is
a smaller hole than seven missing verbs suggests, and it is worth being precise
about it: the executing half of the contract is already on MCP; the filing,
improving and promoting halves are not.

**One number is already at its limit.** The current twelve tools measure 12,496
of the published 12,500-byte schema budget. There is no room for a thirteenth
tool at any description length — the v3 docstrings for `sop_get` and
`sop_revise` were written *down* to fit rather than the budget being raised.
Whatever is decided here, the byte budget has to move or the surface has to
consolidate. That is exactly the revisit condition 0002 wrote: *"the ceiling is
wrong if schema bytes outgrow the count."* It has been reached.

## Options

### A — Raise the ceiling to nineteen, with a restated byte budget

Add all seven as first-class tools. Honest, discoverable, and one tool per verb
keeps every schema small and self-describing.

Estimated cost: seven tools at roughly 550–1,000 bytes of schema each (`run`
is the largest — `inputs`, `bindings`, `version`, `title`, `metadata`), so the
budget moves from 12,500 to about **18,500–19,500 bytes**, a 50% increase in
what every MCP session pays before it does anything.

The argument against is the one 0002 already made: the README justifies the cap
by *context cost*, and this is a 50% increase in that cost paid by every
harness on every session, including the ones that never touch a procedure.

### B — Consolidate into one `sop` tool with an `action` argument

One tool, `action: get | revise | activate | retire | run | outcomes | promote`,
plus a `run` tool for the two read verbs, or `action` covering those too. Tool
count *drops* — twelve becomes eight or nine — and the byte budget shrinks with
it.

The argument against is real and specific. A tool whose parameters are a union
across seven actions has a schema no model can validate against: `bindings` is
required for `run` and meaningless for `retire`, `version` is required for
`activate` and refused for `run`, and the schema cannot say so. The failures
move from "the model did not know the tool existed" — which a model recovers
from by reading the tool list — to "the model called the tool with the wrong
half of its arguments", which it recovers from by guessing. Every refusal
becomes a runtime refusal rather than a schema error. It also breaks the
property the conformance suite is built on: a verb is a verb on every
transport, and a transport that renames seven verbs into one argument is no
longer trivially comparable to the core.

### C — Add two tools, consolidate nothing, and leave the rest on HTTP

Add `sop_run` and `adjudicate` only, raising the ceiling to fourteen and the
byte budget to about 14,000.

The reasoning is that not all seven verbs are equal to a *harness*. `run` and
`adjudicate` are the two an executing harness performs in its own loop: filing
the work it is about to do, and judging a divergence it observed. The other
five are operator verbs — retiring a procedure, promoting a run to a standard,
reading outcomes, listing runs — done by a person at a console or a scheduled
pass, both of which already have HTTP. `sop_create` has been HTTP-only since
the beginning for exactly this reason, and nobody has asked for it.

`run_get` is the one genuine loss: a harness that filed a run cannot read its
own tree back over MCP. But `run` already returns the tree it filed, and the
step beads arrive through `work_pull` with their pins, so the harness has the
same information by a different route.

## Recommendation

**Option C**, and raise the byte budget to 14,000 with the reason stated in
`0002`'s terms.

The ceiling was never about the number twelve. It was about what an MCP session
pays in context before it does anything, and about not letting a plane's whole
API leak onto a surface where every tool is loaded whether it is used or not.
Option A pays a 50% context tax on every session to expose five verbs that
belong to an operator rather than to a harness. Option B lowers the byte cost
by moving the cost somewhere it cannot be measured — into refusals a model
discovers at runtime — and gives up the one-verb-per-tool property that makes
the conformance suite mean what it says.

C keeps the shape 0002 chose (first-class verbs, no union schemas), extends it
by exactly the two verbs a harness performs itself, and states the new byte
number rather than quietly exceeding the old one. If an MCP-only harness later
needs `promote` or `outcomes`, that is a specific request with evidence behind
it, and adding one tool then is a smaller decision than adding five now.

**Revisit condition:** the recommendation is wrong if an MCP-only harness is
observed working around a missing operator verb — shelling out to HTTP,
scripting `agentco` on the side, or asking a person to click something the
plane could have answered. One such report is enough; it means the
harness/operator split above is a distinction the plane made and its users do
not have.

## Not decided here

Nothing changes until this is decided. The MCP surface stays at twelve tools
and inside the 12,500-byte budget, which is where it is today and which the
suite enforces.
