# Roadmap

> Honest about what exists. Nothing below is marked done because it was designed.

## Now — extraction

AgentCo grew up as a private implementation inside one organisation's context. The
coordination primitives are built and tested there; this repository is the extraction of
the parts that are not about any particular company.

The extraction principle: **anything that names a company is configuration, not code.**
The seam turned out to be cleaner than expected — the coordination-layer packages carry
almost no organisation-specific references, and the coupling that does exist concentrated
in exactly the place it should have, the connectors.

| Component | Built | Extracted | Notes |
|---|:--:|:--:|---|
| `leakguard` — the guard that keeps this repo publishable | ✅ | ✅ | Runs in CI and pre-commit |
| Scope claims + conflict detection | ✅ | ✅ | Prefix model, precision self-audit |
| Snapshots + divergence digest | ✅ | ✅ | Pointer-only, cadence-boundary delivery, pluggable resolvers |
| Change feed (opaque resumable cursor) | ✅ | ✅ | |
| HMAC authentication | ✅ | ✅ | Actor from token, never payload |
| Adoption metrics | ✅ | ✅ | Weekly publishers, time-to-first-event, per-verb latency |
| Work queue + fenced leases | ✅ | ✅ | CAS claim + fencing token, self-healing expiry |
| Idempotency (one uniqueness rule on ingest) | ✅ | ✅ | Loud duplicate suppression |
| SOPs as versioned templates | 🆕 | ✅ | Pinned per instance; outcomes grouped by version |
| Scheduling with reservations + silent-schedule audit | ✅ | ⏳ | Catches "this has not run in ten days" |
| Usage metering across harnesses | ✅ | ⏳ | Unreported is `null`, never `0` |
| Health checks with consequence classes | ✅ | ⏳ | Exit code derived from class, never counted |
| MCP surface | ✅ | ⏳ | Thin wrapper over the same core |
| Cross-harness context + lesson sharing | ✅ | ⏳ | |

### Extension points, so connectors never need a core change

Two registries, both following the same shape:

- `snapshots.register_resolver(scheme, fn)` — teach the registry to read one
  URI scheme's version token. A resolver must obtain that token **without
  fetching the body**; a connector that downloads a document to hash it has
  broken the invariant, and no test in core can catch that on its behalf.
- `delivery.register_sender(name, fn)` — deliver a digest somewhere, with
  native formatting. The built-in sender posts plain JSON to a configured URL,
  which is the honest default: a coordination layer has no business knowing
  what any particular chat vendor's card format is.

## Next — the core

- HTTP surface over one semantic core, with a conformance suite.
- Identity beyond HMAC (OIDC), for organisations that want it.
- Change-feed subscriptions with webhook auth.

## Later — review delivery

The routing spine: named owners, shared queues, offers with claim deadlines, explicit
defer reasons, escalation, and a terminal `Undeliverable` state. Capacity modelling ships
as **measurement** — observed and published — and throttles nothing until there is real
data to throttle on.

## Not planned

Writing to a system of record. Any path that does eventually exist will be separately
gated, narrow, and off by default. The whole value proposition is that adopting AgentCo
cannot damage the system you already trust.

---

## The adoption gate

The project's own falsification criterion, written before the result is known:

> **Two identities other than the author publishing weekly, for four consecutive weeks.**

Not "have tried it" — that measures politeness. Four consecutive weeks measures use. A
missed week resets the streak rather than bridging it, and the current week never counts
while it is still running.

If that fails, the honest reading is that coordination across independently-owned
harnesses is not a problem other people have, and this stops at the primitives that are
useful on their own. Stars are the metric that will be available and they are not the one
being used.
