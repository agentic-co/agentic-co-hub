# Changelog

All notable changes to the AgentCo hub are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project aims to follow
[Semantic Versioning](https://semver.org/) from `1.0.0`: [ADR 0011](docs/decisions/0011-v1-definition.md)
defines what version 1 promises (the wire, not `/memory`).

Nothing has been tagged before. `0.1.0` is the version string the package has carried since the
first commit, so "from `0.1.0`" below means "from any earlier checkout of this repository".
Upgrading, migration by migration, with the operator checklist and the rollback:
[`docs/upgrading.md`](docs/upgrading.md). The `asop-spec` versions this release supports are stated
once, in [`docs/upgrading.md`](docs/upgrading.md#supported-asop-spec-range).

## [Unreleased] — to be 1.0.0

### Breaking changes for existing clients and operators

Each of these changes what an existing deployment or client sees. The upgrade notes say what to do
about each one; the short form is here.

**Operators**

- **The verifier registry fails closed.** A `judged` or `human` attestation is accepted only from a
  route declared in `ASOP_VERIFIERS` or a person declared in `ASOP_HUMANS` (the pre-split
  `AGENTCO_VERIFIERS` and `AGENTCO_HUMANS` are still read as a fallback). With neither declared,
  `attest` refuses every such submission with `403 unauthenticated`
  (`tests/test_v1_verifier_fail_closed.py`, including the HTTP route). A hub that never declared a
  registry and relied on it being open must declare one. Deterministic gates are unchanged, and
  filing a gate that only a clock resolves still works. (ADR 0011 decision 2, Finding 4.)
- **Signed query strings are required by default.** The request signature covers the canonical
  query, so a signature made without it on a request that has a query is refused `401`. A client
  that signs the old way breaks. `ASOP_REQUIRE_SIGNED_QUERY=0` (also `false`, `no`, `off`) re-allows
  the old signature for one release; any other value, a typo included, keeps the query required.
  The variable is removed together with the legacy signature after 1.0. Every client shipped in this
  repository already signs the query. (ADR 0011 decision 4, 6b.)
- **Snapshot admission is stricter.** Every redirect hop of an `http(s)` snapshot is admitted before
  it is followed, the connection is made to the address that was vetted, and a redirect from an
  admitted host to a private address that is not allowlisted is recorded as unresolved. Environment
  proxies (`HTTP_PROXY`, `HTTPS_PROXY`) are ignored for snapshot HEAD requests. A hub whose artifact
  server is on a private network must name it in `ASOP_SNAPSHOT_HTTP_HOSTS`. A path that is not a
  regular file (FIFO, directory, device) is recorded as unresolved. (Finding 5, A16.)
- **Shared memory is off unless `AGENTCO_MEMORY=on`, and it is experimental.** It is outside the 1.0
  wire promise: `/memory` and the memory MCP surface may change in a minor release. Any value other
  than `on` or `off` refuses to start.
- **Run ownership epochs now count from 0.** A run never transferred is at epoch 0 and a transfer
  makes it the current epoch + 1; earlier builds wrote 1 at filing. The hub rebases stored epochs
  once at start, marking each record so a second start changes nothing. Events already on the feed
  keep the numbers they were written with.
- **`GET /work` and `GET /work/{id}` are scoped.** A caller who is neither the operator nor a
  transport credential sees items within reach of its party (assignee, holder, gate verifier or
  escalation target, attester, or the filer) plus unassigned items. An item out of reach answers as
  a missing id. Items filed before the filer was recorded keep the assignee rule.
- **A failed authentication no longer writes a `calls` row**, so `/metrics` no longer counts
  refused requests. A request body over 1,000,000 bytes is refused `413 body_too_large` before it is
  read in full.

**Clients**

- **An unknown actor and a bad signature are the same refusal.** Both return one `401
  unauthenticated` with the same code, message and remediation. A client that told the two apart by
  message can no longer do so.
- **Request bodies are strict on the routes that carry a safety property.** An unknown key on
  `POST /work`, `/work/pull`, `/work/{id}/report`, `/work/{id}/attest`, `/work/{id}/adjudicate`,
  `/scope-claims`, `/snapshots`, `/sops` and its revise, activate and run routes, `/chains/manifests`,
  adoptions and `/approvals` is refused `422 unknown_field` instead of being dropped. Other routes
  answer with an `ignored` list. See [`docs/wire.md`](docs/wire.md#request-bodies-strict-and-lenient-routes).
- **Repositories compare case-insensitively.** Scope conflicts compare a repository (and each prefix
  segment) NFC-normalised and case-folded, with trailing dots and spaces dropped per segment, so
  `Acme/App` and `acme/app` now conflict. Responses still echo each party's own spelling.
- **Two new refusals on scope claims, both HTTP 422:** `control_character` for an invisible character (zero-width,
  byte-order mark, bidi mark, soft hyphen and the like) and `bad_path_segment` for a segment made
  only of dots or spaces. They are refused, never stripped.
- **The MCP surface is fourteen tools, within a 14,000-byte schema budget.** `adjudicate` and
  `sop_run` were added to the roster. A harness that pins the tool
  list sees two more tools. A refusal over MCP now carries `structuredContent` with the same
  `{state, code, message, remediation}` body HTTP returns, next to the unchanged text.
- **Claim lifetimes are bounded.** `claim` refuses a TTL of zero or less and clamps a longer one to
  `AGENTCO_LEASE_MAX_AGE` (default 24 hours). `report_result` and `renew` agree: a lapsed lease whose
  attempt is still current can report and renew, and a reclaim bumps the attempt so the old holder is
  refused from then on.
- **Cursors are checked.** A cursor outside 0..2^63-1 is `400 bad_cursor`, and a cursor older than the
  feed's retention floor is `410 cursor_expired` with the resync remedy, never a silent gap.
- **A wrong-typed payload field is a `400` naming the field**, not a `500`.

### Added

- **ASOP v3 records and `asop-spec` 0.4 to 0.6.1.** A procedure is a versioned sequence of steps with
  a gate on each step; chaining fields (`outcomes`, `outputs`, `handoffs`, `on_escalate`, `requires`,
  `entry_gates`, `rework`) are stored, and a record that uses a field the installed contract cannot
  model is quarantined rather than rewritten. Chain manifests, adoptions and approvals are stored and
  chain runs are pinned (#22, #23, #29, #52).
- **Run ownership.** One owner per run, a pack reference, and an administrator transfer
  (`ASOP_ADMINISTRATORS`); new refusals `human_required`, `transfer_refused:<rule>` and
  `manifest_unknown` (#42, #44, #46).
- **The owner's control loop across hubs (ADR 0008).** Operator key directory (enrol, revoke, list
  people's public keys), signed answers to human gates at `POST /answers`, grace confirmation and gate
  reassignment, a generated hub id, a per-actor route allowlist (`routes` in the key table), and a
  transport credential class confined to its own routes (#53, #58, #59, #62, #71, #72, #75).
- **The person's side of the loop.** `agentco partition keygen | pin | pending | answer | sent |
  receipt`, proven between two processes (#93). The runbook for the first two-machine run is
  [`docs/runbooks/first-signed-answer-loop.md`](docs/runbooks/first-signed-answer-loop.md).
- **`GET /views/me`**, one lens per hub (ADR 0010) (#70).
- **Idempotency keys** on `claim_scope`, `snapshot`, `adjudicate`, `sop_revise`, `sop_activate`,
  `attest` and `work_report`, over HTTP and MCP, recorded per actor (migration 0018). The outbox
  drainer passes the line id, so a republish after a crash does not repeat a verb (#88).
- **Lease renew**, the heartbeat of a long step (#47).
- **Change-feed retention.** `agentco prune-events` and `AGENTCO_EVENT_RETENTION` delete a contiguous
  prefix and record the floor; `GET /events` carries `retainedFrom` (#49).
- **Daily roll-up of poll traffic** (`agentco prune`, table `calls_daily`, migration 0010).
- **`holderAttested` on scope conflicts**, so a reader can tell a verified holder from a claimed one.
  Additive on the wire (defect 6a).
- **Shared memory, experimental.** Owner-labeled items with one authoritative hub each, the wall on
  every read path, a usefulness and decay engine, and team spaces inside one hub (ADR 0006, ADR 0009;
  #30, #31, #32, #73). Off by default; see the breaking-changes block.
- **Strict and lenient request bodies, field provenance, and a weekly `asop-spec` watch** that opens
  one issue when a newer release adds fields the hub neither forwards nor stores (#41).
- **Postgres as a storage backend** next to SQLite and the JSONL default (`AGENTCO_DB=postgresql://...`,
  the `postgres` extra), with the same migrations and conformance suite.
- **Quality gates in CI:** pyright over the package, a contract matrix over `asop-spec` releases, a
  Postgres job, an image job that builds and exercises the container, a multi-hub rig on the real
  image, property-based tests and fault-injection tests (#51, #76, #77, #78).
- **A release workflow** that publishes the container image to GitHub Container Registry on a
  `v*.*.*` tag, for `linux/amd64` and `linux/arm64`. See [`docs/releasing.md`](docs/releasing.md).
- **Upgrade notes and a release procedure:** [`docs/upgrading.md`](docs/upgrading.md),
  [`docs/releasing.md`](docs/releasing.md), and `tests/test_upgrade_path.py`, which upgrades a
  registry from the oldest schema to the newest and checks that its rows survive.

### Changed

- **`asop-spec` floor raised to 0.4.0**, pin `>=0.4.0,<1` (#15).
- **The MCP `whoami` tool tests the key** and tells a refused harness how to get access (#16).
- **MCP `claim_scope`, `snapshot`, `sop_revise`, `sop_activate` and `attest` accept
  `idempotency_key`.** A hub older than this release drops an argument it does not declare and gives no
  protection and no error.
- **Gates that could not be evaluated are re-run, not charged** to the step's failure count (spec v3.6
  section 3.3, option A) (#39).
- **Run blockers:** `blockedBy` holds a run's root steps, and the route forwards it (#40).
- **Retried attestation:** a recognised retry applies the adjudication rider if the item has none yet;
  attestations carry the ownership epoch and `verify_attempt` (#54, #61, #88).
- **Federated children** read only their own digests (#55).
- **Event inserts on Postgres take a transaction-scoped advisory lock**, so feed order equals commit
  order, and concurrent migrations on a cold start serialise on one (R16, R17).
- **A naive datetime is read as UTC**, not host-local time, everywhere a timestamp is serialised
  (A13).
- **Storage writes are ordered to fail in the safe direction:** audit rows before the item they
  describe, receipts before the outbox lines they settle, and every append starts a fresh line after a
  torn one (F1 to F4).
- **Clearance and key tables are stat-cached** and re-read when the file changes, so a revocation
  applies on the next request (A14, R15).

### Fixed

- `POST /sops` and revise dropped every ASOP chaining field (#33); a snapshot's `ttl_days` and the
  events tool's `kind` filter were dropped by the remote MCP relay (#37, #38).
- `on_timeout: proceed_on_default` failed the step instead of taking the default (#34); a run refused
  for a self-verifying human step left half a run behind (#35).
- `agentco prune` died with `ImportError` on every run (#48).
- The container image could not run the compose `pg` profile, and its memory store was not on the
  volume (#36, #50).
- The R4 to R21 findings of the 2026-11-07 review: duplicate outbox republish, scope claim and release
  races, a stale-lease report, the unreadable owner table falling back to the actor, a lost
  `blocked_by` entry and an exceeded child cap under concurrent creates, concurrent SOP authoring
  deleting a committed version, a double-credited memory trace after a crash, and a cursor overflow
  ([`docs/known-issues.md`](docs/known-issues.md)).
- `ready()` hid a dependent whose finished blocker had a row this version cannot model, while `claim()`
  would have accepted it; a quarantined work row is now kept as the exact bytes that were read.
- A poisoned work row bricking the queue, a NaN timestamp replay, and a cross-thread dirty read (#87).
- First-time concurrent opening of a new SQLite registry (#91).

### Security

- **Scope evasion closed.** Seven routes (repository case, prefix case, zero-width characters, a
  byte-order mark, NFC versus NFD, a trailing dot, and constructing `Scope()` without validation) no
  longer let a claim avoid a conflict. Existing leases are matched by the same keys at read time, so no
  migration is needed.
- **Snapshot SSRF.** A public URL can no longer redirect the hub's HEAD request to a private address,
  and DNS rebinding between check and connect cannot move the request. See the breaking-changes block.
- **Query-string replay (6b)** closed by default: one captured signed `GET /events` no longer replays
  as any feed query inside the timestamp window.
- **Verifier registry and the actor oracle** as above: an undeclared registry authenticates nobody, and
  the 401 no longer says which actor names exist.
- Transport credentials reach an exact route set and nothing else, and `party_of` fails closed when the
  key table is unreadable (`503 owner_table_unreadable`).

### Removed

- The workspace package `packages/asop`: the contract is its own repository and arrives as the
  `asop-spec` dependency.
- The open-by-default verifier registry, and the legacy unsigned-query signature as a default. The
  latter remains only as the `ASOP_REQUIRE_SIGNED_QUERY=0` opt-out and goes after 1.0.
- A stale known-issue entry: claim 1 (a redirected HEAD became a GET) had been fixed and stayed listed.

### Known limits carried into 1.0

Accepted residues, each with a test or a stated reason, in [`docs/known-issues.md`](docs/known-issues.md):
on the JSONL SOP store `sop_revise` and `sop_activate` record their idempotency key just after the write,
so a crash between the two can repeat one; a transport credential cannot read a run's ownership epoch,
so the first answer after a transfer is refused once and the refusal names the current epoch; a refusal
before the nonce row leaves no receipt; and attestations are HMAC-authenticated requests, not signed
records, so cross-organisation attestation is not claimed.
