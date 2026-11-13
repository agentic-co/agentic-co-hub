# Upgrading from 0.1.0 to 1.0.0

For an operator running a hub built from any `0.1.0` checkout (the version string never changed
before 1.0, and no earlier release was tagged), and for the owners of the clients that talk to it.
What changed, in order of how likely it is to bite, is in [`../CHANGELOG.md`](../CHANGELOG.md); this
page is what to do about it.

Two things are true of every upgrade here and are worth reading before the checklist:

- **There is one migration list and it only moves forward.** Eighteen numbered schema migrations
  (`agentco/migrations.py`), applied automatically the first time a build opens a database, each in
  its own transaction. None has a down step. The way back is the backup you take first.
- **Nothing refuses to open a newer file with an older build.** The ledger records what was applied,
  and a build does not compare it with what it knows. Do not point an older build at a migrated
  database; restore the backup instead.

## Supported `asop-spec` range

The hub depends on the contract package `asop-spec`. **Supported: `>=0.4.0,<1`.**

| | Value | Where it is stated |
|---|---|---|
| Declared dependency range | `>=0.4.0,<1` | `pyproject.toml`, `dependencies` |
| Locked for development and the image | 0.6.1 | `uv.lock` |
| Tested on every change | 0.4.0, 0.4.1, 0.5.0, 0.6.0, 0.6.1 | `.github/workflows/ci.yml`, job `contract` |

The tested set is the floor, its patch release 0.4.1 (the version the execution runtime pins), the
release that once broke the hub (0.5.0), and the two newest. Every released version in the range has
been tested. **The declared range `<1` admits versions newer than the newest one tested:** any 0.6.2,
0.7 or later that is published is installable under the pin before this hub has been run against it,
and the hub makes no claim about it until the `contract` matrix includes it. The weekly `asop-spec
watch` workflow opens an issue when a newer release appears, and raising the tested set is part of
adopting it. Treat a release outside the tested set as unsupported.

## What is migrated: every schema migration

Applied in order by `migrations.apply`, which every opener calls (`agentco serve`, the CLI, the MCP
server, the hook, the pulse). **All eighteen are automatic on start**, with no flag and no separate
command, on both SQLite and Postgres. A migration that fails rolls back and the open fails loudly;
the ledger table `schema_migrations(version, name, applied_at)` records what landed and is never
restamped. **None is reversible.** "Operator action" is only what is needed before the first start
of the new build; most rows have none.

| # | Name | What it changes | Operator action |
|---|---|---|---|
| 0001 | `registry-core` | The registry tables: `meta`, `events`, `leases`, `snapshots`, `calls`, `conflict_actions` and their indexes. This is the schema that existed before there was a ledger, written so applying it to such a file changes nothing, which is what lets an existing registry be adopted instead of rebuilt. On Postgres `events.seq` is an identity column. | None. |
| 0002 | `durable-work-and-sops` | Tables `work_items` (with a partial unique index on `natural_key`) and `sops`, so the database holds the queue and the v2 procedure library. | None. |
| 0003 | `events-agent-label` | `events.agent_label`, nullable: the unverified harness name. | A pre-ledger file that already has this column (only an interim working tree produced one) needs its ledger stamped by hand; otherwise the `ALTER` fails and the open stops. No released build produced such a file. |
| 0004 | `work-item-gates` | `work_items.verify`, `attestation` (nullable; NULL means ungated) and `verify_failures` (default 0), and a partial index on gated items. Every item filed before gates stays ungated. | None. |
| 0005 | `calls-transport-and-label` | `calls.agent_label` and `calls.via`, nullable, and an index on `(via, at)`. Old rows stay NULL because the build cannot say how they arrived. | None. |
| 0006 | `sop-class-tags-authorship` | `sops.executor`, `tags` (default `[]`), `author`, `author_kind`. | None. |
| 0007 | `sop-proposals` | `sops.proposals` (default `[]`). | None. |
| 0008 | `work-items-insertion-order` | `work_items.ordinal`. On Postgres it is `GENERATED ALWAYS AS IDENTITY`, which gives the queue an insertion order that survives updates; on SQLite it is a plain column that is never read (the `rowid` does that job). | On Postgres this rewrites `work_items` under a table lock. Start the new build in a quiet window if the queue is large. |
| 0009 | `asop-v3-records` | New table `asops` (a versioned sequence of steps, a gate on each step), and a **data move**: every v2 `sops` row is copied forward as a one-step ASOP in the same transaction. `sops` is left as it was. | See below: a v2 procedure that was `active` becomes a step gated by a **human**. A row this build cannot model is skipped and stays in `sops`. |
| 0010 | `calls-daily-rollup` | Table `calls_daily`, what `agentco prune` leaves behind when it rolls old poll traffic into a per-day summary. | None. Pruning stays opt-in; nothing is deleted by upgrading. |
| 0011 | `asop-chaining-fields` | Seven `asops` columns for ASOP v3.5 and v3.6: `outcomes`, `outputs`, `handoffs`, `on_escalate`, `requires`, `entry_gates`, `rework`. Existing rows read as "no chaining". All arrive even when the installed contract is 0.4.0. | None. |
| 0012 | `chain-manifests-adoptions-approvals` | Tables `chain_manifests`, `chains`, `chain_adoptions`, `approvals` and their indexes. | None. |
| 0013 | `memory-items` | Tables `memory_items` and `memory_versions`. Created whether or not memory is on; nothing reads them while it is off. | None. |
| 0014 | `memory-usefulness-and-decay` | Tables `memory_state` and `memory_credits`. | None. |
| 0015 | `human-keys` | Table `human_keys`: people's enrolled public keys, kept after revocation. | None. |
| 0016 | `answer-nonces` | Table `answer_nonces` for signed answers: one row per key and nonce. | None. |
| 0017 | `memory-spaces` | `memory_items.space`, default empty (the root). | None. |
| 0018 | `idempotency-keys` | Table `idempotency_keys`, one row per actor, verb and key, newest 500 kept per verb. | None. |

Properties that hold for the whole list, and are tested: each migration runs once and is recorded
(`tests/test_sqlstore.py`); a second start applies nothing and restamps nothing; two processes opening
one fresh file together both finish clean, on SQLite (`tests/test_sqlstore.py`) and on Postgres
(`tests/test_review_r_postgres.py`, under an advisory lock); and a file at the oldest schema reaches
18 with its rows intact (`tests/test_upgrade_path.py`, the proof for this page).

**0009 in practice.** A v2 procedure carried no gate, and a v3 step must have one. The copy chooses the
failing-closed reading: a `human` gate, verified by the version's recorded author (or `operator` when
there is none), checking the old `validation` text, escalating to the same person when its park clock
runs out. Draft and superseded versions cannot run, so only a v2 procedure that was `active` at the
moment of the upgrade is affected. After upgrading, revise each such procedure once to give it the
gate it really has.

**One data change outside the ledger.** Run ownership epochs now count from 0 (a run never transferred
is at epoch 0). The hub rebases stored epochs once, at start, on every backend, marking each record it
has rebased, so a second start, or a crash in the middle, changes nothing. It needs no action, and it
is why a hub started once on the new build should not be rolled back onto old code without restoring
the backup.

### Storage backends

- **SQLite** (`AGENTCO_DB=<path>`, or `AGENTCO_REGISTRY_DB` in the image): the file is migrated in
  place on first open. Everything above applies.
- **Postgres** (`AGENTCO_DB=postgresql://...`, with the `postgres` extra): the same migrations, with
  two dialect rewrites (0001 and 0008). Concurrent cold starts serialise on a schema-keyed advisory
  lock, so starting several replicas against one fresh database is safe. Moving an existing SQLite
  registry to Postgres is a separate act: `tools/migrate_sqlite_to_pg.py`, run against a copy, never
  the live file.
- **JSONL** (the default when `AGENTCO_DB` is unset): there is **no migration and no ledger**. The
  files (`work.jsonl`, `sops.jsonl`, `memory.jsonl`, the outbox) are append-only lines, a field written
  by a newer build survives an older build's update, and a line this build cannot model is quarantined
  rather than rewritten. Upgrading is replacing the code. The epoch rebase above also runs on these
  files. Nothing converts JSONL to SQLite or Postgres for you; choose a backend on a new deployment,
  not as an upgrade step.

## The operator checklist

Do these in order.

1. **Back up, with the hub stopped.**
   - SQLite: stop the process, then copy `registry.sqlite3` together with its `-wal` and `-shm`
     sidecars. In the container: `docker run --rm -v agentco-state:/state -v "$PWD":/out alpine tar czf
     /out/agentco-state.tgz -C /state .` (see [`docker.md`](docker.md#state-backup-and-upgrade)).
   - Postgres: `pg_dump --format=custom --file agentco.dump "$AGENTCO_DB"`.
   - JSONL: copy the store files.
   - Keep the key file (`AGENTCO_REGISTRY_KEYS`) with the backup.
2. **Read the keys.** `agentco keycheck <key-file>` shows what the registry will actually load from the key file.
3. **Declare your verifiers.** Set `ASOP_VERIFIERS` (comma-separated verifier routes, exact spelling) and
   `ASOP_HUMANS` (people) before the first start. With neither set, every judged or human attestation
   is refused `403 unauthenticated`. `AGENTCO_VERIFIERS` and `AGENTCO_HUMANS` are still read when the
   `ASOP_*` name is absent. `ASOP_ADJUDICATORS` declares who may adjudicate, as before.
4. **Decide the other variables.** All names are the ones the code reads:

   | Variable | When to set it |
   |---|---|
   | `ASOP_REQUIRE_SIGNED_QUERY=0` | Only as a stopgap for a client that has not been updated to sign the query. Removed after 1.0. Any other value keeps the query required. |
   | `ASOP_SNAPSHOT_HTTP_HOSTS` | If your artifact server is on a private network: the hosts exempt from the internal-address check. |
   | `ASOP_SNAPSHOT_SCHEMES`, `ASOP_SNAPSHOT_FILE_ROOTS` | If participants snapshot `file:` or `git:` pointers. `ASOP_SNAPSHOT_SCHEMES` defaults to `https,http`; `ASOP_SNAPSHOT_FILE_ROOTS` defaults to no roots. A `file:` or `git:` pointer is recorded but not followed until its scheme is in the first and the directories it may read are in the second. |
   | `AGENTCO_MEMORY=on` | Only to use experimental shared memory. Off by default; any value other than `on` or `off` refuses to start. |
   | `AGENTCO_HUB_ID` | Before enabling signed answers: the name of this hub. |
   | `ASOP_ADMINISTRATORS` | To allow run-ownership transfers. |
   | `AGENTCO_EVENT_RETENTION`, `AGENTCO_LEASE_MAX_AGE` | Optional: feed retention in days (unset keeps everything), and the claim lifetime cap (default 24 hours). |

5. **Update the clients** (below), or set the stopgap in step 4 and update them within the release.
6. **Start the new build.** Migrations run on the first open. Watch the log for the open completing;
   a failure rolls back the migration that failed and leaves the earlier ones applied.
7. **Verify** (next section).

## What clients must change

- **Sign the query string.** A `GET` with a query (`/events?since=...`) must sign the canonical query,
  as the signature's fifth line (`agentco.auth.sign(..., query=...)`). The `Registry` client, the MCP
  server, the outbox drainer and `agentco/publish.py` already do. A hand-written client has to.
- **Stop sending keys the route does not take.** The routes in
  [`wire.md`](wire.md#request-bodies-strict-and-lenient-routes) now answer `422 unknown_field`.
- **Do not rely on the text of a 401.** An unknown actor and a bad signature are one refusal.
- **Branch on `code`, not on prose.** Over MCP, read `structuredContent.code`. The MCP roster is now
  fourteen tools, and an argument a hub does not declare (such as `idempotency_key` on an older hub) is
  dropped silently.
- **Expect more conflicts.** `Acme/App` and `acme/app` are one repository, and a path segment is
  compared case-folded. A claim with an invisible character, or a segment of only dots or spaces, is
  refused (`control_character`, `bad_path_segment`, both HTTP 422).
- **Reading the queue is scoped.** A non-operator caller sees the items in reach of its party.
- **Epochs start at 0.** A client that sent `epoch: 1` for a never-transferred run must send 0 or omit
  it.

## Verify after the upgrade

1. **The schema is at 18.** SQLite: `sqlite3 registry.sqlite3 "SELECT max(version) FROM
   schema_migrations"`. Postgres: `psql "$AGENTCO_DB" -c "SELECT max(version) FROM schema_migrations"`.
   Expect `18`, and 18 rows.
2. **The hub is closed to strangers.** `GET /events` with no credentials must answer `401`. A `200`
   means the key table loaded open. The image's own healthcheck asserts exactly this.
3. **Conformance.** `agentco conform --level L2` (`python3 -m agentco conform --level L2`). It runs
   the scenarios against fresh temporary stores and touches no live database, then checks the MCP
   surface against its budget. A good run ends with `conformant: every transport this level relies on
   is the core` and `MCP surface: 14/14 tools` and exits 0; a departure exits 1 and names itself.
   Use `--level L1` for a publisher that only uses the outbox.
4. **Verifiers are declared.** `agentco verifiers` reports the configured verifier, who has been answering gates, and what is overdue.
5. **A procedure that was active before.** Open a v2 procedure that was active at the upgrade and
   confirm it now shows as a one-step ASOP with a human gate; revise it to its real gate.

## Rolling back

There is no down migration. To go back:

1. Stop the new build.
2. Restore the backup from step 1 of the checklist: the SQLite file with its sidecars, the Postgres
   dump (`pg_restore --clean --if-exists`), or the JSONL files, and the key file.
3. Start the previous build against the restored data.

Anything written after the upgrade is lost by this, including events other hubs may already have
pulled; if other hubs follow this one's feed, tell their operators, because a cursor taken in the
discarded period points past the restored feed and will skip events until the sequence catches up. Do not roll back by starting an older build on the
migrated file, for the two reasons at the top of this page: nothing stops it, and the epoch rebase and
the new columns are not something it understands.
