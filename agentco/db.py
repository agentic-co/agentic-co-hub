"""One SQLite file. That is the whole of stage 1b's storage design.

The storage design, in full: "One SQLite file behind three plain HTTP
endpoints". No apiserver, no admission chain, no RBAC, no resource model —
those are stage 2, and stage 2 only happens if the adoption gate passes. The sequencing
law this obeys is the sequencing law: nine months building a substrate whose 90-day
artifact was four scripts, then discovering the substrate was the reason
nobody adopted the scripts, is the expensive failure the ordering avoids.

Five tables in the one file:

  `events`   — append-only change feed. `seq` is the cursor's only content.
  `leases`   — live ScopeLeases (the scope-model decision (docs/decisions/0001)).
  `snapshots`— pointer + content hash. NEVER a body (docs/architecture.md).
  `calls`    — one row per request, INCLUDING refusals. This is the whole of
               stage 1d: weekly active publishers, time-to-first-event and
               per-verb latency are queries over this table, not a second
               build. A refused call is the most important row in it — a
               colleague whose first three POSTs are refused and who then
               stops is the adoption failure the adoption gate exists to detect, and
               recording only successes would hide exactly that.
  `conflict_actions` — the scope-model decision's self-precision metric: conflicts fired ÷
               conflicts a holder acted on. Below the floor, `k` is wrong.

WAL mode, because the digest job reads while the app writes and a reader
blocking a publisher would show up as latency on the one SLO stage 1 has.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- The change feed. `seq` is AUTOINCREMENT (not plain rowid) so a deleted row
-- can never have its seq reused: a cursor is only resumable if seq is
-- monotonic for the life of the file, not merely unique at any instant.
CREATE TABLE IF NOT EXISTS events (
    seq         INTEGER PRIMARY KEY AUTOINCREMENT,
    uid         TEXT NOT NULL UNIQUE,
    kind        TEXT NOT NULL,
    actor       TEXT NOT NULL,
    repo        TEXT,
    occurred_at TEXT NOT NULL,
    payload     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_actor ON events(actor);
CREATE INDEX IF NOT EXISTS idx_events_kind  ON events(kind);

CREATE TABLE IF NOT EXISTS leases (
    uid            TEXT PRIMARY KEY,
    holder         TEXT NOT NULL,
    holder_attested INTEGER NOT NULL DEFAULT 0,
    repo           TEXT NOT NULL,
    prefixes       TEXT NOT NULL,      -- JSON array of canonical prefixes
    intent         TEXT NOT NULL,
    claimed_at     TEXT NOT NULL,
    expires_at     TEXT NOT NULL,
    released_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_leases_live ON leases(repo, released_at, expires_at);

CREATE TABLE IF NOT EXISTS snapshots (
    uid           TEXT PRIMARY KEY,
    actor         TEXT NOT NULL,
    artifact_uri  TEXT NOT NULL,
    purpose       TEXT NOT NULL,
    hash_kind     TEXT NOT NULL,
    content_hash  TEXT NOT NULL,
    taken_at      TEXT NOT NULL,
    expires_at    TEXT,
    -- Divergence state, updated by the cadence job. `last_seen_hash` differing
    -- from `content_hash` is the divergence; it is NOT delivered until the
    -- next cadence boundary (docs/architecture.md) — real-time pings are
    -- precisely what the people who need this are already drowning in.
    last_seen_hash TEXT,
    last_checked_at TEXT,
    diverged_at    TEXT,
    delivered_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_snapshots_actor ON snapshots(actor);

-- Stage 1d, in one table.
CREATE TABLE IF NOT EXISTS calls (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    verb        TEXT NOT NULL,
    actor       TEXT NOT NULL,
    status      TEXT NOT NULL,        -- accepted | duplicate | refused | error
    code        TEXT,                 -- refusal code, when status = refused
    latency_ms  REAL NOT NULL,
    at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_calls_actor ON calls(actor, at);
CREATE INDEX IF NOT EXISTS idx_calls_verb  ON calls(verb, at);

CREATE TABLE IF NOT EXISTS conflict_actions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    lease_uid   TEXT NOT NULL,
    with_holder TEXT NOT NULL,
    fired_at    TEXT NOT NULL,
    acted_at    TEXT,                 -- set when the holder narrowed/released/coordinated
    action      TEXT                  -- narrowed | released | proceeded | ignored
);
CREATE INDEX IF NOT EXISTS idx_conflict_fired ON conflict_actions(fired_at);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    """Open (creating if needed) the one registry file, schema applied.

    `check_same_thread=False` because uvicorn serves requests on a worker
    thread pool; every write in this package goes through a short `with conn`
    transaction, and SQLite's own locking is the serialisation. Row factory is
    set so callers read columns by name — a positional read is how a schema
    addition silently shifts a field.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()
    return conn
