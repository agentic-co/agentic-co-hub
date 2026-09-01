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

import sqlite3
from datetime import datetime, timezone
from typing import NamedTuple, Sequence


class Migration(NamedTuple):
    version: int
    name: str
    statements: tuple[str, ...]


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


MIGRATIONS: tuple[Migration, ...] = (
    Migration(1, "registry-core", _0001),
    Migration(2, "durable-work-and-sops", _0002),
)


def applied_versions(conn: sqlite3.Connection) -> set[int]:
    conn.execute(LEDGER)
    return {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}


def apply(conn: sqlite3.Connection, pending: Sequence[Migration] = MIGRATIONS) -> list[int]:
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
    """
    done = applied_versions(conn)
    previous = conn.isolation_level
    conn.isolation_level = None
    newly: list[int] = []
    try:
        for migration in pending:
            if migration.version in done:
                continue
            conn.execute("BEGIN IMMEDIATE")
            try:
                for statement in migration.statements:
                    conn.execute(statement)
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
            newly.append(migration.version)
    finally:
        conn.isolation_level = previous
    return newly
