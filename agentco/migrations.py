"""Numbered schema migrations, applied once, recorded in the file itself.

The registry started as one `executescript` of `CREATE TABLE IF NOT EXISTS`
run on every open. That works exactly until the first change that is not a new
table — a column, an index with different semantics, a backfill — at which
point there is nowhere to put it, because nothing anywhere knows what has
already run against this particular file.

So: a numbered list, and a `schema_migrations` table in the same database
recording which numbers have been applied to it. Three properties follow, and
each of them is the reason for one of the tests in `tests/test_sqlstore.py`:

  * **Applied once.** A migration that runs twice is a migration that cannot
    contain a backfill, which means the pattern buys nothing over the
    `IF NOT EXISTS` script it replaces.
  * **One transaction each.** A half-applied migration is the state with no
    recovery path — the ledger says one thing and the schema says another,
    and no later run can tell which statements landed. `BEGIN IMMEDIATE` per
    migration means every file is either at version N or at version N-1.
  * **Adoptable.** Migration 1 is the schema that already exists in every
    deployed file, written so that applying it to such a file changes
    nothing. Upgrading an existing registry must not mean dropping its
    leases, and a migration system that only works on empty files is a
    migration system nobody can adopt.

The ledger records `applied_at` and is never restamped. When something looks
wrong at 02:00, "which version is this file and when did it get there" should
be one query, not an inference from which tables happen to be present.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Callable, NamedTuple, Optional, Sequence


class Migration(NamedTuple):
    version: int
    name: str
    statements: tuple[str, ...]
    # A data move that SQL cannot express honestly. `backfill` runs inside the
    # same transaction as `statements`, immediately after them, and receives
    # the connection. It exists for exactly one shape: a migration whose new
    # rows are a FUNCTION of the old ones rather than a copy of them — the v2
    # SOP → v3 ASOP upgrade has to synthesise a gate the old record never
    # carried, and expressing that decision as nested `json_object()` calls in
    # two SQL dialects would put the reasoning where nobody reads it and the
    # bug where nobody finds it. Still one transaction, so the file is either
    # at version N or at N-1.
    backfill: Optional[Callable[[Any], None]] = None
    # A Postgres-dialect rewrite of `statements`, for the migrations where the
    # SQLite DDL is not portable — `AUTOINCREMENT` (Postgres: `GENERATED
    # ALWAYS AS IDENTITY`) and `INSERT OR IGNORE` (Postgres: `ON CONFLICT ...
    # DO NOTHING`) are the two shapes that appear below. `None` means the
    # SQLite statements already ARE portable (every `CREATE TABLE IF NOT
    # EXISTS`/`ALTER TABLE ADD COLUMN`/`CREATE INDEX IF NOT EXISTS` in this
    # file that has no AUTOINCREMENT column runs unchanged on Postgres) — the
    # common case, so most migrations below carry no second copy at all.
    pg_statements: Optional[tuple[str, ...]] = None


LEDGER = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    applied_at TEXT NOT NULL
)
"""


# --------------------------------------------------------------------------- #
# 0001 — the registry core.
#
# This is the pre-ledger schema, statement for statement. It is reproduced
# rather than referenced so that the migration is a FROZEN historical record:
# a migration that reads its DDL from live code changes meaning when the code
# changes, and then "version 1" names two different schemas depending on when
# you ran it.
# --------------------------------------------------------------------------- #

_0001 = (
    """
    CREATE TABLE IF NOT EXISTS meta (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    # The change feed. `seq` is AUTOINCREMENT (not plain rowid) so a deleted
    # row can never have its seq reused: a cursor is only resumable if seq is
    # monotonic for the life of the file, not merely unique at any instant.
    """
    CREATE TABLE IF NOT EXISTS events (
        seq         INTEGER PRIMARY KEY AUTOINCREMENT,
        uid         TEXT NOT NULL UNIQUE,
        kind        TEXT NOT NULL,
        actor       TEXT NOT NULL,
        repo        TEXT,
        occurred_at TEXT NOT NULL,
        payload     TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_events_actor ON events(actor)",
    "CREATE INDEX IF NOT EXISTS idx_events_kind  ON events(kind)",
    """
    CREATE TABLE IF NOT EXISTS leases (
        uid            TEXT PRIMARY KEY,
        holder         TEXT NOT NULL,
        holder_attested INTEGER NOT NULL DEFAULT 0,
        repo           TEXT NOT NULL,
        prefixes       TEXT NOT NULL,
        intent         TEXT NOT NULL,
        claimed_at     TEXT NOT NULL,
        expires_at     TEXT NOT NULL,
        released_at    TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_leases_live ON leases(repo, released_at, expires_at)",
    """
    CREATE TABLE IF NOT EXISTS snapshots (
        uid           TEXT PRIMARY KEY,
        actor         TEXT NOT NULL,
        artifact_uri  TEXT NOT NULL,
        purpose       TEXT NOT NULL,
        hash_kind     TEXT NOT NULL,
        content_hash  TEXT NOT NULL,
        taken_at      TEXT NOT NULL,
        expires_at    TEXT,
        last_seen_hash TEXT,
        last_checked_at TEXT,
        diverged_at    TEXT,
        delivered_at   TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_snapshots_actor ON snapshots(actor)",
    """
    CREATE TABLE IF NOT EXISTS calls (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        verb        TEXT NOT NULL,
        actor       TEXT NOT NULL,
        status      TEXT NOT NULL,
        code        TEXT,
        latency_ms  REAL NOT NULL,
        at          TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_calls_actor ON calls(actor, at)",
    "CREATE INDEX IF NOT EXISTS idx_calls_verb  ON calls(verb, at)",
    """
    CREATE TABLE IF NOT EXISTS conflict_actions (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        lease_uid   TEXT NOT NULL,
        with_holder TEXT NOT NULL,
        fired_at    TEXT NOT NULL,
        acted_at    TEXT,
        action      TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_conflict_fired ON conflict_actions(fired_at)",
    "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', '1')",
)


# The Postgres rewrite of `_0001`, statement for statement, differing only
# where SQLite's dialect is not portable:
#
#   * `INTEGER PRIMARY KEY AUTOINCREMENT` → `BIGINT GENERATED ALWAYS AS
#     IDENTITY PRIMARY KEY` on the three append-only tables (`events.seq`,
#     `calls.id`, `conflict_actions.id`) — Postgres's identity column, chosen
#     over `SERIAL` because it is the SQL-standard spelling and does not leave
#     a same-named sequence object implicitly owned in older, surprising ways.
#   * `INSERT OR IGNORE` → `INSERT ... ON CONFLICT (key) DO NOTHING`.
#
# Every other line — every `CREATE TABLE IF NOT EXISTS` with a TEXT primary
# key, every `CREATE INDEX IF NOT EXISTS`, every partial index — is copied
# unchanged, because it already IS portable and a second hand-typed copy of
# an unchanged line is a second place for the two to drift.
_0001_pg = (
    """
    CREATE TABLE IF NOT EXISTS meta (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS events (
        seq         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        uid         TEXT NOT NULL UNIQUE,
        kind        TEXT NOT NULL,
        actor       TEXT NOT NULL,
        repo        TEXT,
        occurred_at TEXT NOT NULL,
        payload     TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_events_actor ON events(actor)",
    "CREATE INDEX IF NOT EXISTS idx_events_kind  ON events(kind)",
    """
    CREATE TABLE IF NOT EXISTS leases (
        uid            TEXT PRIMARY KEY,
        holder         TEXT NOT NULL,
        holder_attested INTEGER NOT NULL DEFAULT 0,
        repo           TEXT NOT NULL,
        prefixes       TEXT NOT NULL,
        intent         TEXT NOT NULL,
        claimed_at     TEXT NOT NULL,
        expires_at     TEXT NOT NULL,
        released_at    TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_leases_live ON leases(repo, released_at, expires_at)",
    """
    CREATE TABLE IF NOT EXISTS snapshots (
        uid           TEXT PRIMARY KEY,
        actor         TEXT NOT NULL,
        artifact_uri  TEXT NOT NULL,
        purpose       TEXT NOT NULL,
        hash_kind     TEXT NOT NULL,
        content_hash  TEXT NOT NULL,
        taken_at      TEXT NOT NULL,
        expires_at    TEXT,
        last_seen_hash TEXT,
        last_checked_at TEXT,
        diverged_at    TEXT,
        delivered_at   TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_snapshots_actor ON snapshots(actor)",
    """
    CREATE TABLE IF NOT EXISTS calls (
        id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        verb        TEXT NOT NULL,
        actor       TEXT NOT NULL,
        status      TEXT NOT NULL,
        code        TEXT,
        latency_ms  REAL NOT NULL,
        at          TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_calls_actor ON calls(actor, at)",
    "CREATE INDEX IF NOT EXISTS idx_calls_verb  ON calls(verb, at)",
    """
    CREATE TABLE IF NOT EXISTS conflict_actions (
        id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        lease_uid   TEXT NOT NULL,
        with_holder TEXT NOT NULL,
        fired_at    TEXT NOT NULL,
        acted_at    TEXT,
        action      TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_conflict_fired ON conflict_actions(fired_at)",
    "INSERT INTO meta(key, value) VALUES ('schema_version', '1') "
    "ON CONFLICT (key) DO NOTHING",
)


# --------------------------------------------------------------------------- #
# 0002 — work items and SOPs, so the durable backend holds everything.
#
# Columns mirror the `WorkItem` and `SOP` dataclasses one for one, with the
# list/dict fields as JSON text. Not a single opaque `payload` blob: the
# moment `ready()` or `outcomes_by_version()` wants a WHERE clause, a blob
# means reading every row into Python to filter it, which is the JSONL store
# with extra steps.
#
# `unknown` is the forward-compatibility seam, and it exists because the JSONL
# store already promised it (`Queue._merge`): a field written by a NEWER
# version must survive an older one's routine update rather than being deleted
# by it. Fields this version does not model land there and are never touched
# by an UPDATE, which is the same promise expressed in a schema.
# --------------------------------------------------------------------------- #

_0002 = (
    """
    CREATE TABLE IF NOT EXISTS work_items (
        id               TEXT PRIMARY KEY,
        title            TEXT NOT NULL,
        status           TEXT NOT NULL,
        assigned_agent   TEXT,
        requires         TEXT NOT NULL DEFAULT '[]',
        blocked_by       TEXT NOT NULL DEFAULT '[]',
        leased_by        TEXT,
        lease_attempt    INTEGER NOT NULL DEFAULT 0,
        lease_expires_at TEXT,
        result           TEXT,
        natural_key      TEXT,
        metadata         TEXT NOT NULL DEFAULT '{}',
        created_at       TEXT NOT NULL,
        updated_at       TEXT NOT NULL,
        unknown          TEXT NOT NULL DEFAULT '{}'
    )
    """,
    # The idempotency rule as a constraint rather than a convention. The JSONL
    # store enforces it by scanning before it appends, which is correct only
    # because an exclusive file lock is held across both halves. Here the
    # index is the rule and the scan is an optimisation — a second writer that
    # somehow skipped the check still cannot create the duplicate.
    #
    # Partial, because "no natural key" is the common case and NULLs must not
    # collide with each other.
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_work_natural_key "
    "ON work_items(natural_key) WHERE natural_key IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_work_status ON work_items(status)",
    # `trigger` is a SQLite keyword and is quoted everywhere it appears. Named
    # to match the dataclass field anyway: a column renamed to dodge a keyword
    # is a mapping layer that has to be remembered, and it will not be.
    """
    CREATE TABLE IF NOT EXISTS sops (
        sop_id             TEXT NOT NULL,
        version            INTEGER NOT NULL,
        title              TEXT NOT NULL,
        status             TEXT NOT NULL,
        purpose            TEXT,
        "trigger"          TEXT,
        entry_check        TEXT,
        inputs             TEXT,
        definition_of_done TEXT,
        validation         TEXT,
        write_back         TEXT,
        common_mistakes    TEXT NOT NULL DEFAULT '[]',
        next_sop           TEXT,
        superseded_by      INTEGER,
        created_at         TEXT NOT NULL,
        unknown            TEXT NOT NULL DEFAULT '{}',
        PRIMARY KEY (sop_id, version)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_sops_status ON sops(sop_id, status)",
)


# --------------------------------------------------------------------------- #
# 0003 — `events.agent_label`, the unverified harness name.
#
# The first migration that is not a new table, which is the case the ledger was
# built for: `CREATE TABLE IF NOT EXISTS events` in migration 1 is frozen, so a
# column added there would change what "version 1" means and still never reach
# a file already in use.
#
# Nullable on purpose. A label is self-reported and optional, and every read
# path treats absent and empty as the same thing — an actor that reported no
# harness name. It is never promoted to an authenticated fact (see
# `docs/decisions/0002-participation-ladder.md`), so nothing joins on it and no
# index is warranted.
#
# The one file this cannot be applied to is a pre-ledger file that ALREADY has
# the column, which no released build produces — only an interim working tree
# that added it as a startup side effect. Such a file needs its ledger stamped
# by hand; the alternative, an ALTER whose failure is swallowed, would make
# every genuine migration failure silent to buy that.
# --------------------------------------------------------------------------- #

_0003 = ("ALTER TABLE events ADD COLUMN agent_label TEXT",)


# --------------------------------------------------------------------------- #
# 0004 — the gate: `verify`, its evidence, and its failure count.
#
# `verify` and `attestation` are nullable JSON text, and the NULL is load
# bearing rather than merely permitted: absent means UNGATED, which is what
# every item filed before gates existed is. That is the whole of the legacy
# scope guard — no backfill, no flood of suddenly-unverified work — so a
# DEFAULT '{}' here would quietly gate the entire existing backlog.
#
# `verify_failures` is NOT NULL DEFAULT 0 because "how many times has this
# item's gate said no" has a correct answer for an ungated item, and it is
# zero. A nullable counter would make every reader handle a None that cannot
# happen.
# --------------------------------------------------------------------------- #

_0004 = (
    "ALTER TABLE work_items ADD COLUMN verify TEXT",
    "ALTER TABLE work_items ADD COLUMN attestation TEXT",
    "ALTER TABLE work_items ADD COLUMN verify_failures INTEGER NOT NULL DEFAULT 0",
    # The queue's questions about gates are all of the form "what is waiting on
    # a verdict" — a partial index, because gated items are the minority and an
    # index over the ungated majority's NULLs pays for nothing.
    "CREATE INDEX IF NOT EXISTS idx_work_gated ON work_items(status) "
    "WHERE verify IS NOT NULL",
)


# --------------------------------------------------------------------------- #
# 0005 — how a call reached us, and which harness said it made it.
#
# The adoption question the participation-ladder ADR makes a revisit condition
# — "the ladder is wrong if L1 does not convert" — is not answerable from the
# columns `calls` had. The drainer signs as the MACHINE, so an outbox publish
# and a direct one were the same row shape, and the harness behind the outbox
# appeared nowhere in this table at all.
#
# Both columns are SELF-REPORTED and neither is covered by the signature.
# That is acceptable here and worth stating rather than discovering later: the
# threat model for an adoption metric is "am I fooling myself", not "is an
# attacker lying" — nobody has an incentive to forge their own adoption data.
# They get the same treatment `events.agent_label` gets, which is to be
# recorded, reported, and never promoted to an authenticated fact.
#
# Nullable, because every row written before this migration reached us somehow
# and this build cannot say how. A DEFAULT 'direct' would invent history:
# millions of rows asserting a fact nobody observed.
# --------------------------------------------------------------------------- #

_0005 = (
    "ALTER TABLE calls ADD COLUMN agent_label TEXT",
    "ALTER TABLE calls ADD COLUMN via TEXT",
    "CREATE INDEX IF NOT EXISTS idx_calls_via ON calls(via, at)",
)


# --------------------------------------------------------------------------- #
# 0006 — the step's class, its tags, and who wrote each version.
#
# The revision policy (agentco/policy.py) needs three things the SOP row did not
# carry: which steps are human, which are protected, and who authored each
# version. `tags` is NOT NULL DEFAULT '[]' because `_row_to_sop` decodes JSON
# columns unconditionally and a NULL there would quarantine every pre-existing
# row — the same reason `common_mistakes` was declared that way. The other three
# are nullable: an existing version has no recorded author and no class, and a
# default of 'agent' or 'human' would assert a fact nobody observed. The policy
# treats an unrecorded author as imposing nothing and an unclassified step as
# an agent step, which is the conservative reading of each.
# --------------------------------------------------------------------------- #

_0006 = (
    "ALTER TABLE sops ADD COLUMN executor TEXT",
    "ALTER TABLE sops ADD COLUMN tags TEXT NOT NULL DEFAULT '[]'",
    "ALTER TABLE sops ADD COLUMN author TEXT",
    "ALTER TABLE sops ADD COLUMN author_kind TEXT",
)


# --------------------------------------------------------------------------- #
# 0007 — `sops.proposals`: revision proposals accumulating against the template.
#
# One line per `good` adjudication, written by `SopLibrary.propose` onto the
# draft it creates and carried forward until a reviser addresses or dismisses
# it. NOT NULL DEFAULT '[]' for the reason `tags` is: JSON columns are decoded
# unconditionally on read and a NULL would quarantine every pre-existing row.
# --------------------------------------------------------------------------- #

_0007 = ("ALTER TABLE sops ADD COLUMN proposals TEXT NOT NULL DEFAULT '[]'",)


# --------------------------------------------------------------------------- #
# 0008 — `work_items.ordinal`: a portable stand-in for SQLite's `rowid`.
#
# `SqlQueue._read_raw` orders by `rowid` so `list()`/`ready()` return items in
# INSERTION order regardless of how many times a row has since been UPDATEd —
# SQLite's rowid does not move on UPDATE, only on VACUUM/re-insert, neither of
# which this codebase does. Postgres has no such column: `ctid` is the closest
# analogue and is explicitly the wrong one — it changes on every UPDATE
# (MVCC writes a new tuple version), so ordering by it would reorder the queue
# every time a lease is claimed or a result reported, not just on create.
#
# `ordinal` is populated only for Postgres (`GENERATED ALWAYS AS IDENTITY`,
# which Postgres backfills for existing rows on `ADD COLUMN`) and is simply
# never read on SQLite, where `rowid` already does this job — the SQLite
# statement below adds the column for schema symmetry (so a row from either
# backend has the same column set) but nothing ever queries it there.
# --------------------------------------------------------------------------- #

_0008 = ("ALTER TABLE work_items ADD COLUMN ordinal INTEGER",)
_0008_pg = ("ALTER TABLE work_items ADD COLUMN ordinal BIGINT GENERATED ALWAYS AS IDENTITY",)


# --------------------------------------------------------------------------- #
# 0009 — the ASOP v3 record: a sequence of steps, each with its own gate.
#
# A NEW table rather than columns on `sops`, for two reasons that both come
# down to the grain changing. `sops.inputs` is prose ("what you need in hand")
# and `asops.inputs` is a list of declared input NAMES a run must supply —
# same word, different question, and one column cannot hold both without a
# reader having to know which generation wrote the row. And half of `sops`'
# columns (entry_check, definition_of_done, validation, write_back,
# common_mistakes, executor, tags) moved ONTO the step, so they would have had
# to be nulled out in place, destroying the legacy record this migration is
# obliged to keep readable.
#
# `sops` is therefore left exactly as it is — every row still there, still
# byte-identical — and `_backfill_legacy_sops` COPIES each row forward as a
# one-step ASOP. What the plane reads afterwards is `asops`; `sops` is the
# provenance, and the thing a rollback would still have.
#
# `steps` is NOT NULL DEFAULT '[]' for the reason `tags` was in 0006: the JSON
# columns are decoded unconditionally on read and a NULL would quarantine the
# row rather than fail loudly.
# --------------------------------------------------------------------------- #

_0009 = (
    """
    CREATE TABLE IF NOT EXISTS asops (
        asop_id       TEXT    NOT NULL,
        version       INTEGER NOT NULL,
        title         TEXT    NOT NULL,
        status        TEXT    NOT NULL,
        task_type     TEXT,
        purpose       TEXT,
        "trigger"     TEXT,
        inputs        TEXT    NOT NULL DEFAULT '[]',
        roles         TEXT    NOT NULL DEFAULT '{}',
        "constraints" TEXT    NOT NULL DEFAULT '[]',
        steps         TEXT    NOT NULL DEFAULT '[]',
        author        TEXT,
        author_kind   TEXT,
        proposals     TEXT    NOT NULL DEFAULT '[]',
        superseded_by INTEGER,
        created_at    TEXT    NOT NULL,
        unknown       TEXT    NOT NULL DEFAULT '{}',
        PRIMARY KEY (asop_id, version)
    )
    """,
    "CREATE INDEX IF NOT EXISTS asops_status ON asops (status)",
)


def _backfill_legacy_sops(conn) -> None:
    """Copy every v2 `sops` row forward as a one-step ASOP.

    Runs inside migration 9's transaction. `upgrade_legacy` owns the one
    decision this cannot avoid making — a v2 record carried no gate, and a v3
    step must have one — and its docstring says why it fails closed to a human
    gate rather than inventing a check nobody wrote.

    A row this build cannot model is SKIPPED, not fatal: it stays in `sops`,
    where it already was, and the migration does not take the whole registry
    down over one row a newer writer left behind.
    """
    from agentco.sop import upgrade_legacy
    from asop.sop import SOP, SopStatus

    existing = {(r[0], r[1]) for r in conn.execute("SELECT asop_id, version FROM asops")}
    rows = conn.execute("SELECT * FROM sops").fetchall()
    for row in rows:
        data = dict(row)
        if (data.get("sop_id"), data.get("version")) in existing:
            continue
        try:
            legacy = SOP(
                sop_id=data["sop_id"],
                version=data["version"],
                title=data["title"],
                status=SopStatus(data["status"]),
                purpose=data.get("purpose"),
                trigger=data.get("trigger"),
                entry_check=data.get("entry_check"),
                inputs=data.get("inputs"),
                definition_of_done=data.get("definition_of_done"),
                validation=data.get("validation"),
                write_back=data.get("write_back"),
                common_mistakes=json.loads(data.get("common_mistakes") or "[]"),
                next_sop=data.get("next_sop"),
                executor=data.get("executor"),
                tags=json.loads(data.get("tags") or "[]"),
                author=data.get("author"),
                author_kind=data.get("author_kind"),
                proposals=json.loads(data.get("proposals") or "[]"),
                superseded_by=data.get("superseded_by"),
                created_at=data["created_at"],
            )
            asop = upgrade_legacy(legacy)
        except (KeyError, TypeError, ValueError):
            continue
        conn.execute(
            'INSERT INTO asops (asop_id, version, title, status, task_type, purpose, '
            '"trigger", inputs, roles, "constraints", steps, author, author_kind, '
            'proposals, superseded_by, created_at, unknown) '
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                asop.asop_id, asop.version, asop.title, asop.status.value, asop.task_type,
                asop.purpose, asop.trigger,
                json.dumps([dict(r) for r in asop.inputs]),
                json.dumps(asop.roles),
                json.dumps(asop.constraints),
                json.dumps([_asdict(s) for s in asop.steps]),
                asop.author, asop.author_kind,
                json.dumps(asop.proposals), asop.superseded_by, asop.created_at,
                data.get("unknown") or "{}",
            ),
        )


def _asdict(step) -> dict:
    from dataclasses import asdict as _dc_asdict

    return _dc_asdict(step)


MIGRATIONS: tuple[Migration, ...] = (
    Migration(1, "registry-core", _0001, pg_statements=_0001_pg),
    Migration(2, "durable-work-and-sops", _0002),
    Migration(3, "events-agent-label", _0003),
    Migration(4, "work-item-gates", _0004),
    Migration(5, "calls-transport-and-label", _0005),
    Migration(6, "sop-class-tags-authorship", _0006),
    Migration(7, "sop-proposals", _0007),
    Migration(8, "work-items-insertion-order", _0008, pg_statements=_0008_pg),
    Migration(9, "asop-v3-records", _0009, backfill=_backfill_legacy_sops),
)


def applied_versions(conn) -> set[int]:
    """`conn` is a `sqlite3.Connection` or a `pgadapter.PgConnection` — this
    function only ever calls `.execute()`, so the type is left unannotated
    rather than importing `pgadapter` here for a Union that buys no checking
    (nothing in this module constructs either type)."""
    conn.execute(LEDGER)
    return {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}


def apply(conn, pending: Sequence[Migration] = MIGRATIONS) -> list[int]:
    """Bring `conn`'s file up to date. Returns the versions newly applied.

    An empty return is the normal steady state and is what the idempotency
    test asserts — "nothing to do" has to be distinguishable from "did it all
    again", and a function that returns nothing cannot say which happened.

    `isolation_level` is forced to autocommit for the duration and restored
    after, because callers hold this connection afterwards and use `with conn:`
    for their own transactions. Forcing it here is not a preference: with
    Python's legacy implicit-transaction handling, an explicit `BEGIN
    IMMEDIATE` inside an already-open implicit transaction raises, and the
    whole point of the per-migration transaction is that it is explicit.

    **The decision to run a migration is taken under the lock, not before it.**
    The first read of the ledger is an optimisation — it lets a fully migrated
    file skip straight to returning `[]` without taking a write lock at all —
    but it is read with no lock held, so it is only ever a hint. Two processes
    opening the same FRESH file both see an empty ledger; whichever loses the
    race for `BEGIN IMMEDIATE` would then re-run a migration that has just been
    applied. The DDL is `IF NOT EXISTS` and survives that, but the ledger INSERT
    hits the primary key, and the loser's whole open fails with an
    `IntegrityError` — on the ordinary cold start where two services pointed at
    the same `AGENTCO_DB` come up together. So the version is re-checked inside
    the transaction, where the answer cannot change under us, and an
    already-applied migration is skipped rather than repeated.
    """
    # `dialect` is read off the connection, not passed in — every caller
    # already does `conn = db.connect(target)` / `conn = pgadapter.connect(...)`
    # before reaching here, and a plain `sqlite3.Connection` has no `dialect`
    # attribute, which is exactly the "sqlite" default. One call site
    # (`migrations.apply(conn)`) stays unchanged for both backends this way.
    dialect = getattr(conn, "dialect", "sqlite")
    done = applied_versions(conn)
    previous = conn.isolation_level
    conn.isolation_level = None
    newly: list[int] = []
    try:
        for migration in pending:
            if migration.version in done:
                continue
            statements = (
                migration.pg_statements
                if dialect == "postgres" and migration.pg_statements is not None
                else migration.statements
            )
            conn.execute("BEGIN IMMEDIATE")
            try:
                # The authoritative check: the write lock is held, so no other
                # process can apply this version between here and the COMMIT.
                already = (
                    conn.execute(
                        "SELECT 1 FROM schema_migrations WHERE version = ?",
                        (migration.version,),
                    ).fetchone()
                    is not None
                )
                if not already:
                    for statement in statements:
                        conn.execute(statement)
                    if migration.backfill is not None:
                        # Inside the same BEGIN IMMEDIATE as the DDL above, so
                        # the schema and the data it derives arrive together or
                        # neither does.
                        migration.backfill(conn)
                    conn.execute(
                        "INSERT INTO schema_migrations(version, name, applied_at) "
                        "VALUES (?, ?, ?)",
                        (
                            migration.version,
                            migration.name,
                            datetime.now(timezone.utc).isoformat(),
                        ),
                    )
            except BaseException:
                # Loudly, and all the way out. A migration that fails halfway
                # and is swallowed leaves a file whose ledger disagrees with
                # its schema, and every later run then makes it worse.
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
            done.add(migration.version)
            if not already:
                # Only what THIS call applied. The loser of the race must not
                # report having migrated a file it found already migrated —
                # the return value is what a caller logs, and two processes
                # both claiming to have applied version 1 is the same
                # ambiguity the ledger exists to remove.
                newly.append(migration.version)
    finally:
        conn.isolation_level = previous
    return newly
