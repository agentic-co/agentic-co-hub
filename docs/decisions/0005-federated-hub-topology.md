# 0005 — Hubs can federate, and federation is optional

**Status:** proposed · **Date:** 2026-09-19

> A decision with no revisit condition is doctrine. Every record here carries one.

## Context

The registry today is flat by construction: every connecting agent, whatever
its participation level (0001, "Participation is a ladder"), talks to exactly
one registry instance. That's fine for one team. It stops being fine at
company scale — `docs/participation.md`'s own scaling note for L2 already
assumes a handful of connections, and a single registry serving every team's
agents at once turns a hundred-plus L2 workers into one blast radius, one
noisy neighbor problem, and one place where a team's minute-to-minute churn
pollutes whatever a board/CTO-level view is trying to read off the same feed.

The principal's framing (2026-09-19): a company should be able to run a
**layered** structure — a corporate hub holding company-level goals, with each
team running its own hub underneath, each team's agents connecting to their
own team hub rather than the corporate one directly. Ten teams of ten beat one
hub of a hundred.

The hard requirement alongside it: **federation must be optional, per hub, at
any granularity.** A single team must be able to stand up a hub with zero
upward connection — no parent, no company hub, nothing — because most orgs
adopting this will not have it running everywhere at once. The ladder's own
philosophy already states this for participation levels ("nobody has to adopt
all four at once, and the levels compose... against the same registry") — this
decision extends the same compositional promise to the space *between*
registries, not just within one.

## Alternatives

**(a) Stay flat.** One registry per org, scale it by hoping teams stay small
and few. Simplest, but it's the exact centralization this project's own stated
posture (distributed over centralized, per-node rather than one hub store)
argues against, and it has a real ceiling `docs/participation.md` already
gestures at.

**(b) A new hub-to-hub protocol.** Design a distinct wire format for
hub-to-hub sync — different from how a leaf agent talks to its hub. More
purpose-built, but it doubles the surface the conformance suite (`agentco
conform`) has to hold to one semantic core, and creates two protocols to keep
from drifting apart, which is exactly the kind of duplication this project's
`asop-spec`-arrives-by-version discipline exists to avoid elsewhere.

**(c) A parent hub is just another actor on the child hub's own registry** —
recursion, not a new concept. The same `hub pull / status / sync` surface a
leaf uses to talk to its hub is what a hub uses to talk to the hub above it.
Federating one level up costs exactly what connecting a worker costs today: a
`hub.url` and a signed identity, nothing new to build twice.

## Decision

**(c).** A hub is symmetric: it always runs `serve` for whoever connects
beneath it, and *optionally* also runs as a client (`hub pull/status/sync`)
against a `hub.url` above it. Unset that URL — which is already every
registry's default state — and the hub is standalone, exactly as a lone team's
registry is today. Federation is a hub deciding to also be a worker one level
up; it is never a precondition for the hub working locally.

Three subordinate decisions follow from it.

**Rollup, not replication.** A child hub does not mirror its raw event stream
upward — that reintroduces the blast-radius problem one level higher. It
reports through the existing `digest`/`pulse` cadence machinery
(`agentco digest`, `agentco pulse` — already built for cadence-boundary
divergence reporting), pointed at the parent instead of a human channel. The
parent sees a periodic summary per child, not a live firehose; the corporate
view is eventually consistent by design, on the same cadence a team already
uses for its own digest.

**The child hub is the unit the parent knows about, not the child's agents.**
A team's hub connects to the company hub as one named actor (its own
`keygen`'d identity — "team-frontend", say), the same way one agent connects
to a team hub today. The parent's registry size scales with team count, never
with agent count — this is what actually solves "a hundred connected to one":
nobody above the team layer ever sees more than one connection per team.

**Disconnecting is free and lossless.** Because the upward link is a worker
connection like any other, unsetting `hub.url` on a team hub costs nothing
locally — its own registry, its own agents' L0-L3 participation, its own
`digest`/`pulse` all keep working exactly as before. Nothing about local
operation was ever contingent on the parent existing. This is the same
zero-cost-to-disconnect property L0 already has for an individual agent,
extended one layer up.

## Consequences

- A company can run this partially from day one: some teams federated to a
  corporate hub, others standalone, others not running any of this at all —
  same "compose, don't mandate" posture as the participation ladder itself.
- Scope naming (`claim_scope("acme/web-platform", ...)`) needs a convention
  for what a *team* hub reports at company scope vs. what stays team-internal
  — not decided here; likely answered by whatever a digest's summary shape
  ends up being, which is separable follow-up work, not a blocker to (c).
- **(1) Built 2026-09-19.** `POST /digests` (`agentco/digests.py`) records a
  child's rollup as a `DigestReceived` event — no new read surface, the parent
  reads it back through the existing `GET /events` cursor. `Registry.digest`
  (`publish.py`) and a new `--via hub` built-in sender (`delivery.py`,
  `post_to_hub`, opt-in via `AGENTCO_REGISTRY_URL`/`AGENTCO_ACTOR`/
  `AGENTCO_SECRET` — the same three variables that already connect a worker to
  a hub) close the loop. Verified end-to-end against a real listening parent
  instance, not mocked at the transport (`tests/test_digests.py`); full suite
  1325 passed/18 xfailed, `conform --level L2` still 12/12 MCP tools (this
  work added no MCP surface, deliberately — see the roadmap's 12-tool ceiling).
- **(2)** still open: a conformance case exercising a hub acting as BOTH
  server (to children) and client (to a parent) in one process. The unit and
  live-server tests above prove each half works; nothing yet proves them
  composing inside a single `agentco serve` instance simultaneously.

## Revisit condition

Revisit if a real deployment needs a *live* (not digest-cadence) view at the
parent level — e.g., a company-wide incident dashboard that can't tolerate a
cadence delay. That would argue for an event-forwarding option alongside the
digest rollup, not replacing it.
