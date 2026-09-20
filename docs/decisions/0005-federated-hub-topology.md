# 0005 — Hubs can federate, and federation is optional

**Status:** accepted · **Date:** 2026-09-19 (updated 2026-09-20 after independent
cross-vendor review — GPT-5.4 via codex and Gemini 3.1 Pro via agy — found real
defects in the first pass; see Consequences)

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
recursion, not a new concept. Reuse the signed-request client (`Registry`,
`publish.py`) a leaf agent already uses to talk to its hub, pointed one level
up. Federating one level up costs exactly what connecting a worker costs
today: a URL and a signed identity, nothing new to build twice.

## Decision

**(c).** A hub is symmetric: it always runs `serve` for whoever connects
beneath it, and *optionally* also files its own cadence-boundary digest with a
hub above it — `agentco digest --deliver --post --via hub`, the exact command
a team already runs to post its digest anywhere else, pointed at a parent
instead of a webhook. Unset the three env vars that name the parent, and the
hub is standalone, exactly as a lone team's registry is today. Federation is a
hub choosing to also be a worker one level up; it is never a precondition for
the hub working locally.

⚠️ **Correction (2026-09-20, found in review):** this section originally said
federation reused "the same `hub pull/status/sync` surface a leaf uses to talk
to its hub." No such CLI surface exists — `agentco` has no `hub` subcommand at
all. What was actually built, and is the real Decision, is narrower and
PUSH-ONLY: a child files its digest upward; nothing flows back down. A parent
polling or pushing WORK to its children (the fuller symmetry the original text
implied) is a different, unbuilt feature — if it's ever wanted, it is new
scope, not something this ADR already covers.

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
- **(1) Built 2026-09-19, hardened 2026-09-20 after review.** `POST /digests`
  (`agentco/digests.py`) records a child's rollup as a `DigestReceived` event
  — no new read surface, the parent reads it back through the existing
  `GET /events` cursor. `Registry.digest` (`publish.py`) and a `--via hub`
  built-in sender (`delivery.py`, `post_to_hub`) close the loop.

  **Independent review (GPT-5.4 via codex, Gemini 3.1 Pro via agy) found the
  first pass genuinely unsafe, not just imperfect.** Both, from different
  angles, converged on the same real gap: nothing distinguished a declared
  team hub from an ordinary correctly-signed leaf agent, so the ADR's own
  scaling argument ("the parent's registry grows with team count, never agent
  count") was a convention nobody enforced. Fixed: `ASOP_FEDERATED_CHILDREN`
  (legacy `AGENTCO_FEDERATED_CHILDREN`) is a declared allowlist a parent must
  set before `POST /digests` accepts anything from anyone — **fails CLOSED**,
  unlike `humans`/`verifiers`, because an empty-but-permissive default here
  would defeat the entire mechanism (see `digests.py`'s module docstring).

  Other real defects found and fixed: a failed upward send used to mark the
  local pointer as reported ANYWAY (`divergence.deliver` ran before
  `delivery.send`), so "eventually consistent" was actually silent
  at-most-once loss — `cli.py`'s `cmd_digest` now sends first and only marks
  on success, and returns exit 1 with a message instead of an uncaught
  traceback on failure. A non-JSON 200 response (the single likeliest
  misconfiguration — wrong URL, a proxy, an SSO portal) used to leak a raw
  `JSONDecodeError` from `Registry._call`; it now raises `RegistryError` like
  every other refusal. The three env vars originally reused for "the hub
  above me" (`AGENTCO_REGISTRY_URL`/`AGENTCO_ACTOR`/`AGENTCO_SECRET`) turned
  out to already mean "proxy every local MCP tool over HTTP" in
  `mcp_server.py` — renamed to `AGENTCO_PARENT_HUB_URL`/`_ACTOR`/`_SECRET` so
  federating a team hub cannot silently break that team's own local MCP.
  `meta` is now passed through by the built-in sender (movedCount /
  stuckGateCount) instead of silently dropped, and both `text` and `meta` are
  now type-checked (a non-string `text` or non-object `meta` used to reach a
  bare 500 "this is a registry bug" instead of a clean refusal).

  Re-verified after the fixes, not just before: full suite 1333 passed/18
  xfailed (up from 1325 — 8 new tests covering the fixes), `conform --level
  L2` still 12/12 MCP tools (deliberately no new MCP surface — see the
  roadmap's 12-tool ceiling), leakguard clean on every changed file.

- **(2) Resolved, not just left open — verified 2026-09-20.** The original
  text flagged "a hub acting as BOTH server and client at once" as untested.
  Checked directly rather than assumed: `delivery.send` is called from exactly
  one place (`cli.py`'s `cmd_digest`, a short-lived CLI invocation), nothing in
  `app.py` imports `delivery`, and `divergence.collect` reads only the
  `snapshots` table — it never reads the event feed at all, so a
  `DigestReceived` event cannot feed the next digest and there is no
  feedback-loop or amplification risk. The two roles never actually run in the
  same process at the same time; they share a database file, not a runtime.
  No code change was needed — this was a documentation-accuracy fix once
  someone actually traced the call graph instead of leaving the question open.

## Revisit condition

Revisit if a real deployment needs a *live* (not digest-cadence) view at the
parent level — e.g., a company-wide incident dashboard that can't tolerate a
cadence delay. That would argue for an event-forwarding option alongside the
digest rollup, not replacing it.
