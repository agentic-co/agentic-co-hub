"""The upgrade path `docs/upgrading.md` promises: oldest schema in, newest schema out, rows intact.

ADR 0011 gate 5 asks for "upgrade notes from `0.1.0` covering every migration".
A note is only as good as the run behind it, so this is that run. It builds a
registry the way the OLDEST supported build left it (migration 1's tables and no
ledger at all), puts rows in it, lets the build under test open it, and checks
three things an operator would otherwise find out in production:

  * every migration is applied and the ledger says so (`schema_migrations`);
  * rows written at the old schema are still there, unchanged, and a column
    added later reads as the default the migration documents rather than NULL
    where the code expects a value;
  * the one migration that moves data (0009, the v2 SOP -> v3 ASOP copy) did
    so, and left the old table as it was.

The file is staged in two steps on purpose. Writing the rows only at the oldest
schema would never exercise 0009's backfill, which needs a `sops` row, and a
`sops` row cannot exist before migration 2. So rows are added at the oldest
schema AND again at schema 8, the last one before the data move.
"""

from __future__ import annotations

import json
import os
import sqlite3

import pytest

from agentco import db, migrations

OLDEST = migrations.MIGRATIONS[0]
LAST = migrations.MIGRATIONS[-1].version
BEFORE_ASOP = [m for m in migrations.MIGRATIONS if m.version <= 8]
AFTER_ASOP = [m for m in migrations.MIGRATIONS if m.version > 8]

PG = os.environ.get("AGENTCO_TEST_PG")
needs_pg = pytest.mark.skipif(not PG, reason="AGENTCO_TEST_PG is not set")

# One representative row per table that exists at the oldest schema.
OLD_ROWS = (
    "INSERT INTO leases (uid, holder, repo, prefixes, intent, claimed_at, expires_at) "
    "VALUES ('l-old','alice','acme/app','[\"src/one\"]','implement',"
    "'2026-01-01T00:00:00+00:00','2099-01-01T00:00:00+00:00')",
    "INSERT INTO events (uid, kind, actor, repo, occurred_at, payload) "
    "VALUES ('evt-old','ScopeClaimed','alice','acme/app','2026-01-01T00:00:00+00:00','{\"n\":1}')",
    "INSERT INTO calls (at, actor, verb, status, latency_ms) "
    "VALUES ('2026-01-01T00:00:00+00:00','alice','claim_scope','accepted',3.5)",
)

# What exists at schema 8: a work item (before gates) and a v2 SOP (before ASOP v3).
MID_ROWS = (
    "INSERT INTO work_items (id, title, status, created_at, updated_at) "
    "VALUES ('w-old','an item filed before gates','pending',"
    "'2026-01-02T00:00:00+00:00','2026-01-02T00:00:00+00:00')",
    "INSERT INTO sops (sop_id, version, title, status, purpose, created_at) "
    "VALUES ('sop-old', 1, 'a v2 procedure', 'active', 'ship it', '2026-01-02T00:00:00+00:00')",
)


def _ledger(conn) -> list[int]:
    return [r[0] for r in conn.execute("SELECT version FROM schema_migrations ORDER BY version")]


def _assert_upgraded(conn) -> None:
    # The final schema version is the last migration, and nothing was skipped.
    assert _ledger(conn) == list(range(1, LAST + 1))
    assert _ledger(conn) == [m.version for m in migrations.MIGRATIONS]

    # Rows from the oldest schema survive untouched.
    lease = conn.execute("SELECT holder, repo, released_at FROM leases WHERE uid='l-old'").fetchone()
    assert (lease["holder"], lease["repo"], lease["released_at"]) == ("alice", "acme/app", None)
    event = conn.execute("SELECT kind, agent_label, payload FROM events WHERE uid='evt-old'").fetchone()
    assert event["kind"] == "ScopeClaimed" and event["agent_label"] is None
    assert json.loads(event["payload"]) == {"n": 1}
    call = conn.execute("SELECT verb, via, agent_label FROM calls WHERE actor='alice'").fetchone()
    assert call["verb"] == "claim_scope" and call["via"] is None and call["agent_label"] is None

    # A pre-gate work item is UNGATED (verify NULL), not suddenly unverified (0004).
    item = conn.execute(
        "SELECT title, status, verify, attestation, verify_failures FROM work_items WHERE id='w-old'"
    ).fetchone()
    assert item["title"] == "an item filed before gates" and item["status"] == "pending"
    assert item["verify"] is None and item["attestation"] is None and item["verify_failures"] == 0

    # 0009: the v2 row is still in `sops`, and a one-step ASOP now exists beside it.
    sop = conn.execute("SELECT title, tags, proposals FROM sops WHERE sop_id='sop-old'").fetchone()
    assert sop["title"] == "a v2 procedure"
    assert json.loads(sop["tags"]) == [] and json.loads(sop["proposals"]) == []
    asop = conn.execute("SELECT title, status, steps FROM asops WHERE asop_id='sop-old' AND version=1").fetchone()
    assert asop is not None and asop["title"] == "a v2 procedure" and asop["status"] == "active"
    assert len(json.loads(asop["steps"])) == 1

    # Tables added after the oldest schema exist and start empty.
    for table in ("calls_daily", "chain_manifests", "chains", "chain_adoptions", "approvals", "memory_items",
                  "memory_versions", "memory_state", "memory_credits", "human_keys", "answer_nonces",
                  "idempotency_keys"):
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0, table

    # Columns added by later migrations are readable on the old rows.
    conn.execute('SELECT outcomes, outputs, handoffs, on_escalate, "requires", entry_gates, rework FROM asops')
    conn.execute("SELECT space FROM memory_items")


def test_sqlite_from_the_oldest_schema_to_the_newest_keeps_every_row(tmp_path):
    path = tmp_path / "oldest.sqlite3"

    # 1. The oldest schema: migration 1's tables, no ledger.
    raw = sqlite3.connect(str(path))
    raw.row_factory = sqlite3.Row
    raw.executescript(";\n".join(OLDEST.statements) + ";")
    for statement in OLD_ROWS:
        raw.execute(statement)
    raw.commit()
    assert not raw.execute("SELECT name FROM sqlite_master WHERE name='schema_migrations'").fetchall()

    # 2. Up to schema 8 under the ledger, then the rows that cannot exist earlier.
    assert migrations.apply(raw, pending=BEFORE_ASOP) == [m.version for m in BEFORE_ASOP]
    for statement in MID_ROWS:
        raw.execute(statement)
    raw.commit()
    raw.close()

    # 3. The build under test opens it: every remaining migration runs, on start, with no flag.
    conn = db.connect(path)
    _assert_upgraded(conn)

    # 4. A second start applies nothing and restamps nothing.
    stamps = {r["version"]: r["applied_at"]
              for r in conn.execute("SELECT version, applied_at FROM schema_migrations")}
    assert migrations.apply(conn) == []
    again = {r["version"]: r["applied_at"]
             for r in conn.execute("SELECT version, applied_at FROM schema_migrations")}
    assert again == stamps


@needs_pg
def test_postgres_from_the_oldest_schema_to_the_newest_keeps_every_row(_pg_dsn):
    from agentco import pgadapter

    conn = pgadapter.connect(_pg_dsn)
    try:
        for statement in OLDEST.pg_statements or OLDEST.statements:
            conn.execute(statement)
        for statement in OLD_ROWS:
            conn.execute(statement)
        assert migrations.apply(conn, pending=BEFORE_ASOP) == [m.version for m in BEFORE_ASOP]
        for statement in MID_ROWS:
            conn.execute(statement)
        assert migrations.apply(conn) == [m.version for m in AFTER_ASOP]
        _assert_upgraded(conn)
    finally:
        conn.close()
