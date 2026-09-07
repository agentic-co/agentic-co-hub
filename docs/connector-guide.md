# Connecting a custom harness over HTTP

For a harness that speaks neither MCP nor Python: everything below was observed
on the wire against a live registry, with a Claude Code session on stdio MCP as
the other participant, on 2026-09-03. Where the docs and the wire disagreed, the
wire is what is written here.

The short version: sign each request with HMAC-SHA256, send camelCase field
names, treat every non-2xx as a refusal with a `code` and a `remediation`, and
never report against a lease attempt you were not issued.

## Authentication

```
X-AgentCo-Actor:     your-harness
X-AgentCo-Timestamp: 1756900000               # unix seconds, ±300s of the server
X-AgentCo-Signature: hex(HMAC-SHA256(secret, "{METHOD}\n{path}\n{timestamp}\n{sha256hex(body)}"))
Content-Type:        application/json
```

- `path` is signed **without** the query string (`GET /events?limit=1` signs `/events`).
- An empty body hashes as the sha256 of zero bytes. Send no body on `GET`.
- The actor is whatever the signature authenticates. A payload that names an
  `actor` is refused (`400`); a harness that wants to say which tool it is uses
  `agentLabel`, which travels as unverified.
- Unauthenticated → `401`. That is also the health check: a `200` with no
  credentials means the key file failed open.
- Secrets come from `agentco keygen your-harness`, merged by the operator into
  the JSON file `AGENTCO_REGISTRY_KEYS` points at. The reference signer is
  `agentco/publish.py::_sign`, thirty lines of standard library.

## Wire casing

Request fields are **camelCase**: `assignedAgent`, `naturalKey`, `blockedBy`,
`sourceId`, `ttlSeconds`, `idempotencyKey`. Response items come back
**snake_case** (`lease_attempt`, `assigned_agent`, `natural_key`). The server
ignores unknown request fields, so a snake_case key on a request is dropped
silently and the item is filed without it — the Python client refuses that
spelling before sending for exactly this reason. Do the same in yours.

## The responses you will see

Every write answers `{"state": "accepted", ...}` or a refusal:

```json
{"state": "refused", "code": "work_conflict", "message": "…", "remediation": "…"}
```

| Status | `code` | When |
|---|---|---|
| 401 | — | signature or timestamp wrong |
| 400 | `actor_in_body`, `bad_json`, … | you tried to name yourself in the payload |
| 409 | `work_conflict` | report against a lease attempt that is no longer current |
| 409 | `duplicate` / `state: "duplicate"` | same idempotency key or natural key, already done — not an error |
| 422 | `scope_too_broad` | a claim prefix with fewer than 2 path segments |
| 422 | `decomposition_bound` | more than 7 children or deeper than 3 |
| 404 | `unknown_item` | the item id does not exist |

Read `remediation` and show it to a person. It is written to be shown.

## The calls a work-queue seam needs

Observed shapes, trimmed to the fields a connector reads.

**Pull** — `POST /work/pull` `{"ttlSeconds": 600, "capabilities": ["…"]}`

```json
{"state": "leased", "attempt": 1, "item": {"id": "w-8b996cd3", "status": "in_progress",
  "leased_by": "your-harness", "lease_expires_at": "2026-09-03T13:35:31+00:00", "title": "…",
  "requires": [], "blocked_by": [], "verify": null, "metadata": {}}}
```

`{"state": "empty"}` means nothing claimable. The queue is filtered by
`assigned_agent` (yours or unset) and by `requires ⊆ capabilities`. There is no
"pull only this goal" parameter; scope claims are unrelated to which work a pull
returns. Default TTL is 3600s; a 5s lease worked, so pick what your worker's
step actually takes and renew by finishing.

**Report** — `POST /work/{id}/report` `{"attempt": 1, "status": "done", "result": "…"}`

```json
{"state": "accepted", "item": {"id": "…", "status": "done", "lease_attempt": 2, "leased_by": null,
  "metadata": {"claims": [{"agent": "your-harness", "attempt": 1, "at": "…"}],
               "lease_report": {"attempt": 1, "reported_by": "your-harness", "status": "done", "reported_at": "…"}}}}
```

Terminal statuses are `done` and `failed` only. Note `lease_attempt` advanced
on the report — every terminal event and every reap revokes the number with the
lease. **The fence, as observed:** a worker whose 5s lease lapsed was reaped by
the pulse; its report on attempt 1 answered `409 work_conflict` ("the item is on
attempt 2"); its next pull was issued attempt 3 and that report was accepted.
Your `Report` must surface a `409` as *superseded*, never as *recorded*.

**Create** — `POST /work` `{"title": "…", "naturalKey": "…", "assignedAgent": "…", "verify": {…}}`
answers `{"state": "accepted", "item": {…}}`. A repeated `naturalKey` returns
the existing item with `state: "duplicate"`.

**List** — `GET /work` or `GET /work?ready=1` → `{"state": "accepted", "items": [...]}`.
`ready=1` includes items whose lease has lapsed: they are claimable whether or not
a reaper ran. That is by design — recovery never waits for a sweep.

**Heartbeat and release have nothing to call.** There is no lease-renew route
and no work release; a worker that will overrun its lease reports `failed` (or
lets it lapse and re-pulls, paying an attempt). Model `HeartbeatAsync` as a
no-op returning false, and `ReleaseAsync` as "let it lapse".

## Gates, if your items carry one

A `verify` object on create is validated at the boundary — `kind`
(`deterministic|judged|human`), `check`, `max_park_seconds`, `on_timeout`
(`pass|fail|escalate`), plus `escalate_to` when escalating and `verifier` on a
human gate. Unknown keys are refused.

Observed on a judged gate with `on_timeout: escalate, max_park_seconds: 60`:

1. the executor's `done` report answered `status: "awaiting_verify"` and stamped
   `metadata.verify_parked_at` — **done was a request, not an outcome**;
2. no event reached the feed until the pulse's routing pass ran (it now runs on
   every `agentco pulse --apply`; before that fix a parked gate was invisible);
3. 61s later the pulse escalated it: `metadata.verify_escalated = {by: "park-clock",
   to: "…", note: "resolved by the declared default, NOT by a verdict — no check was run"}`,
   a `GateEscalated` event, status still `awaiting_verify`;
4. `POST /work/{id}/attest` from a **different** actor with
   `{"attestation": {"check": "…", "exit_status": 0, "environment": "…", "at": "…"}, "capabilities": ["verify"]}`
   landed `done`, `attestation.submitted_by` naming the attester.

A deterministic gate wants the attestation on the report itself; judged and
human gates refuse one from the executor. Once the operator declares
`AGENTCO_VERIFIERS`, `verify` counts only for those actors.

## Procedures: what a bead tells you about the ASOP it came from

A bead filed by `work_create` carries no procedure. A bead produced by
instantiating an ASOP carries **`metadata.sop_ref`**, and this is the field
most likely to cost you an afternoon:

```json
"metadata": {
  "sop_ref": {"asop_id": "asop-6da6c50d", "version": 2, "step": 1},
  "sop_plan": { "...": "the step's own text, copied at instantiate" },
  "parent":   "w-9ba21ba0"
}
```

`asop_id`, **not** `sop_id`. The `/sops/{id}` resource has its own natural key
spelled `sop_id`; a bead's pin does not. Two independently written readers in the
same codebase both guessed `sop_id`, both compiled, both passed their own unit
tests — because the tests asserted the same wrong name — and both returned a null
pin against a live registry for weeks. If you take one thing from this document,
take this: **read a real payload before you write the parser.**

The same trap on the procedure itself. An ASOP is a **versioned sequence of gated
steps**, and almost everything a connector wants is per-step, not top level:

```
top level   asop_id, version, title, task_type, purpose, trigger, status,
            inputs (a LIST of {name, description}, not a string), roles,
            constraints, steps, proposals, author, author_kind, created_at,
            superseded_by
per step    name, step, role, purpose, entry_check, definition_of_done,
            validation, write_back, common_mistakes, gate, after, inputs
            (free text here), uses, tags, proposals
```

`entry_check`, `definition_of_done`, `validation`, `write_back`,
`common_mistakes` and `gate` all live **inside a step**. A reader modelled on a
flat one-procedure-one-gate shape does not error on this — it returns a procedure
with every instructional field empty, which reads as a procedure that says
nothing rather than as a parse failure.

Two more shapes worth knowing before you assume:

- **A gate carries `check` (a string) OR `checks` (a list).** Both occur in the
  same library. A reader that handles one silently nulls the other.
- **`after` is how step order is expressed** — `"after": [1]` on step 2. There is
  no `next_sop`; inter-procedure sequencing was deliberately removed from the
  contract and belongs to the harness. A connector that expects to find chaining
  in the artefact will not find it.

### Check the round trip, not just the parse

Every failure above is the same one: a field that MOVED — renamed, or relocated
into a step — read at its old address and returning **null instead of raising**.
Null is what makes it survive; three of these degraded silently for as long as
nothing looked.

Neither habit that feels like diligence catches it. A unit test written beside
the reader replays the reader's own assumption. Validating a payload on the way
IN asks whether the store will accept it, never whether your reader gets it back.

So do both directions: read a procedure from the registry, read it back through
your own consumer, and diff the populated fields. `tools/roundtrip_check.py` in
this repo does exactly that against any consumer URL and exits non-zero on a
drop. Pointed at a connector for the first time it found six dropped fields in
under two minutes, one of them the gate — which is to say the connector could
read what the work was and not how it would be judged.

## Scope claims and the feed

`POST /scope-claims` `{"repo": "org/repo", "prefixes": ["src/billing/"], "intent": "refactor"}`
answers the lease **and** any overlapping live claims in the same response —
`conflicts: [{withHolder, theirIntent, overlaps: [{mine, theirs}]}]`. Observed
across transports: a claim over HTTP saw the conflict with a claim made over
stdio MCP seconds earlier. Prefixes need at least two path segments (`422
scope_too_broad` otherwise); `intent` is one of the registry's declared set
(`prototype|implement|review|refactor` by default).

`GET /events?since=<cursor>&limit=200` → `{"events": [...], "nextCursor": "…", "count": n}`.
Store `nextCursor` verbatim and pass it back; never construct one. Kinds you will
care about: `ScopeConflict`, `WorkParked`, `GateEscalated`, `DivergenceObserved`,
`PulseObserved`.

## What the operator runs beside you

- `agentco pulse --apply --every 15m` on a schedule ([`pulse.md`](pulse.md)).
  It reaps lapsed leases, routes and clocks gates, and flags actors silent past
  their declared cadence. Ask the operator to declare yours:
  `AGENTCO_CADENCE="your-harness=2h"`. Over HTTP every call you make counts as
  liveness, empty pulls included.
- The pulse's exit code is the worst class present, never a count. If your
  harness runs its own monitor, alert on `1` and `2`, not on the number of lines.

## Checklist for a connector

1. HMAC signer + the four headers; treat `401` on a signed request as a clock or
   key problem, not a server one.
2. camelCase on the way out; snake_case on the way in.
3. `Pull` → `(item, attempt, lease_expires_at)`; `Report(id, attempt, status)`;
   `409` → superseded. Never retry a report with the same attempt.
4. Refusals are `{code, message, remediation}`; log the remediation.
5. `ready=1` for visibility, never for choosing — the server chooses on pull.
6. If you send `verify`, be ready for `awaiting_verify` on your own `done`.
7. Persist the events cursor per harness identity.
