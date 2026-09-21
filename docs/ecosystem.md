# The ecosystem, in about seven minutes

What AgentCo is, what a hub is, what a harness is, and what each one is *for*.
Written to be read aloud — the version you give someone who asks what you are
building, and the version you re-read when a week of engineering has pulled the
goal out of shape.

---

## The problem, which is not a technology problem

Everyone on your team is already running an AI coding agent. Different tools,
different vendors, no two configured the same way, and **nobody is giving theirs
up**. That last part is the whole design constraint. Any answer that begins
"first, everyone standardises on…" has already failed, because it requires the
one thing you cannot get.

Individually those agents work fine. Together they are blind to each other, and
it shows up in three ordinary ways. Two agents edit the same directory and
nobody finds out until the merge. One builds against a spec that changed last
Tuesday. And an agent hits something it cannot decide, asks a human, and the
question goes nowhere — because nothing guaranteed it was delivered to anyone.

None of those is a model problem. A smarter agent does not fix any of them,
because the missing thing is not intelligence. It is *shared state between
people and agents who are not in the same process*.

## What AgentCo holds

Three things, and deliberately only three.

**The claims people and agents make about each other's work.** Before you start,
you say which repository and which paths you are working in. If that overlaps
somebody's live claim, both sides are told, with both intents attached, at the
moment it happens rather than at the merge.

**The pointers you built against.** Not the documents — the *pointers*, with a
version token. Nothing is ever copied into the plane. When the thing you built
against moves, you are told it moved.

**A router that puts a decision in front of a named human and records the
acknowledgement.** Not a notification that may or may not be read. A question
with somebody's name on it and a recorded answer.

## The organizing idea: an ASOP

An **Agentic Standard Operating Procedure** is a procedure with three properties
an ordinary SOP does not have.

It is **versioned** — outcomes are recorded per version, never per vibe. When a
procedure gets better you can say *how much*, because you have the before.

It is **verified** — it carries its own definition of done. A deterministic
check, a fixed rubric, or a named human's sign-off, enforced where completion is
recorded, so that no executor grades its own homework. This is the load-bearing
one. A step whose gate is prose is read by the same party it is meant to
constrain, which is how a confident agent reports success it did not have.

It is **self-revising** — the divergence between what was planned and what
actually happened feeds the next version, instead of a postmortem nobody reads.

The single sentence: *an SOP tells an agent what to do; an ASOP can prove it was
done, and gets better when it wasn't.*

## Hub and harness — the distinction everything else rests on

**The harness is where work happens.** It is your agent runtime — Claude Code,
Codex, Antigravity, whatever you already run, whatever you switch to next year.
It edits files, runs tests, calls tools. It is yours, it is heterogeneous, and
it stays that way.

**The hub is where agreement lives.** It executes nothing. It holds the claims,
the pointers, the procedures and the verdicts, and it is the one thing everyone's
different runtime talks to.

The shortest way to say it: **the harness does the work; the hub holds what the
work has to agree with.**

That split decides several things that look like implementation details and are
not. A gate *evaluates in the harness*, because a plane cannot run your test
suite on your machine — the hub carries the gate's definition and records the
verdict, and the harness is what actually runs the check. A procedure authored
by one team is published *to* the hub and arrives at everyone else's harness,
so a lesson learned once does not have to be learned again in each runtime. And
the hub never needs to know what tool you use, which is what lets the answer
survive your team changing tools.

## The ladder, because adoption is the hard part

If participating costs a config change, most people never participate, and a
coordination layer with three participants coordinates nothing. So
participation is four levels and a harness declares its own:

An **observer** reads a spliced block of context — zero cost, works in a
runtime nobody configured. A **publisher** writes to a local file that a drainer
signs and ships — still zero cost to the person. A **worker** pulls work,
reports it, and attests — one config line. A **verifier** answers judged gates —
deliberate setup, and deliberately not everyone.

The floor is the point. You get value before you configure anything, which is
the only way the tenth person joins.

## Federation, and what it is for

A hub can also be a worker to another hub. A team runs its own, files its
cadence digest upward, and a parent hub gives the organisation a rollup without
ever seeing the team's individual claims or work.

Federation is for how teams are *organised* — autonomy, blast radius, separate
ownership. It is not the answer to connection volume; that question is answered
by the access layer inside a single hub, and it was measured rather than
assumed. Reach for federation when a team should own its own plane, not when
you are worried about load.

## What success actually looks like

Not a request rate. The adoption gate is **two publishers other than the
operator, sustained for four consecutive weeks**, with the current week excluded
because a gate that oscillates is not a decision instrument.

That number is the honest one because it measures the thing that is hard. Making
this serve more traffic is engineering. Making a second and third person
*choose* to keep publishing to it, for a month, without being told to, is the
product working.

## Where the first real deployment fits

The first tenant is not the product. It is one organisation of roughly forty
people across engineering, product and quality, with two or three agents each —
somewhere near a hundred and thirty connected actors. That is where the design
meets turnover, shared repositories, procedures somebody actually follows, and a
deployment that can be down.

The hub stays the thing being built. A tenant is how you find out which parts of
it were true.

*(Naming that organisation here would put a company name in a repository whose
founding rule is that anything naming a company is configuration rather than
code. The tenant's own deployment repo is where its name belongs.)*
