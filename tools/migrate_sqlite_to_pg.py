#!/usr/bin/env python3
"""Copy one SQLite registry + `work.jsonl` + `sops.jsonl` into a Postgres database.

    python3 tools/migrate_sqlite_to_pg.py \
        --sqlite-db /tmp/copy/registry.sqlite3 \
        --work-jsonl /tmp/copy/work.jsonl \
        --sop-jsonl /tmp/copy/sops.jsonl \
        --pg-dsn postgresql://user:pass@host:5432/agentco

**Never point this at the live files.** `agentco/db.py` opens the SQLite file
in WAL mode and holds a connection for the life of a server process; a copy
made while that process is running is exactly as safe as `cp` always is for
a WAL database (SQLite's own online-backup guarantee), which is why this
tool takes paths rather than assuming `~/.agentco/` — copy first, migrate
the copy, never the original.

**Idempotent.** Every insert is `ON CONFLICT DO NOTHING` keyed on the same
uniqueness the source already enforces (`events.uid`, `leases.uid`,
`snapshots.uid`, `work_items.id`, `asops.(asop_id, version)`, and a
content-addressed key for `calls`/`conflict_actions`, which the SQLite
schema leaves otherwise unconstrained). Running this twice against the same
source and target changes nothing the second time — printed counts show
`inserted: 0` throughout.

**The feed cursor keeps resuming.** `events.seq` is an identity column on
Postgres (migration 0001's `pg_statements`); a plain `INSERT` would refuse
an explicit `seq` or silently issue a NEW one, either of which breaks a
subscriber's `since=<old seq>` the moment its cursor crosses the migration.
This tool inserts with `OVERRIDING SYSTEM VALUE` to keep the exact source
`seq`, then advances the underlying sequence past the highest value
inserted (`_bump_identity`) so the next EVENT WRITTEN AFTER migration gets
the next number in the same series — never a value the copy already used.
`calls.id`/`conflict_actions.id` get the same treatment, for the same
reason applied to a column nothing in this codebase currently reads back
by value, but which is worth keeping faithful to the source anyway.

**What this tool is not**: a sync daemon, a schema translator (the target
schema is created by the SAME `agentco.migrations` module the SQLite path
uses — `db.connect`/`SqlQueue`/`SqlSopLibrary` apply it on open, same as
always), or a dry-run previewer. It is meant to be run once, against a
stopped source, as the cutover step from `AGENTCO_DB=<sqlite path>` to
`AGENTCO_DB=<postgres dsn>`.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import fields
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentco import db  # noqa: E402
from agentco.pgadapter import is_postgres_target  # noqa: E402
from agentco.sop import ASOP, SopLibrary  # noqa: E402
from agentco.sqlstore import ASOP_COLUMNS, WORK_COLUMNS, _asop_to_row, _item_to_row  # noqa: E402
from agentco.work import Queue, WorkItem  # noqa: E402

# `calls`/`conflict_actions` have no natural key in the SQLite schema — no
# UNIQUE constraint at all, since nothing has ever needed to dedupe a
# telemetry row. Idempotency for them is keyed on every OTHER column: two
# rows equal in everything but `id` are the same event by construction (the
# same request cannot have been recorded twice with different content), so
# a partial unique index on the non-identity columns is created here, once,
# to give `ON CONFLICT` something to key on. It is created IF NOT EXISTS so
# a second run finds it already there.
_CALLS_DEDUPE_INDEX = (
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_calls_migrate_dedupe "
    "ON calls(verb, actor, status, COALESCE(code, ''), latency_ms, at, "
    "COALESCE(agent_label, ''), COALESCE(via, ''))"
)
_CONFLICT_ACTIONS_DEDUPE_INDEX = (
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_conflict_actions_migrate_dedupe "
    "ON conflict_actions(lease_uid, with_holder, fired_at)"
)


def _bump_identity(conn, table: str, column: str) -> None:
    """Advance `table.column`'s identity sequence past its current MAX.

    `is_called=true` in the `setval` call means the NEXT `nextval()` — the
    next row this table gets from ordinary application traffic after the
    migration — returns MAX+1, never a value this tool just inserted with
    `OVERRIDING SYSTEM VALUE`. Safe to call on an empty table too: `MAX`
    over zero rows is `NULL`, `COALESCE` makes it `1`, and `is_called=false`
    for that one case means the untouched sequence still starts at 1 next.
    """
    row = conn.execute(f"SELECT MAX({column}) AS m, COUNT(*) AS n FROM {table}").fetchone()
    if row["n"] == 0:
        return
    conn.execute(
        f"SELECT setval(pg_get_serial_sequence('{table}', '{column}'), {int(row['m'])}, true)"
    )


def copy_events(src, dst) -> dict:
    rows = src.execute("SELECT * FROM events ORDER BY seq").fetchall()
    inserted = 0
    for r in rows:
        cur = dst.execute(
            "INSERT INTO events (seq, uid, kind, actor, agent_label, repo, occurred_at, payload) "
            "OVERRIDING SYSTEM VALUE VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (uid) DO NOTHING",
            (r["seq"], r["uid"], r["kind"], r["actor"], r["agent_label"], r["repo"], r["occurred_at"], r["payload"]),
        )
        if cur.rowcount and cur.rowcount > 0:
            inserted += 1
    _bump_identity(dst, "events", "seq")
    max_seq = dst.execute("SELECT MAX(seq) AS m FROM events").fetchone()["m"]
    return {"source_rows": len(rows), "inserted": inserted, "max_seq": max_seq}


def copy_leases(src, dst) -> dict:
    rows = src.execute("SELECT * FROM leases").fetchall()
    inserted = 0
    for r in rows:
        cur = dst.execute(
            "INSERT INTO leases (uid, holder, holder_attested, repo, prefixes, intent, "
            "claimed_at, expires_at, released_at) VALUES (?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT (uid) DO NOTHING",
            (r["uid"], r["holder"], r["holder_attested"], r["repo"], r["prefixes"], r["intent"],
             r["claimed_at"], r["expires_at"], r["released_at"]),
        )
        if cur.rowcount and cur.rowcount > 0:
            inserted += 1
    return {"source_rows": len(rows), "inserted": inserted}


def copy_snapshots(src, dst) -> dict:
    rows = src.execute("SELECT * FROM snapshots").fetchall()
    inserted = 0
    for r in rows:
        cur = dst.execute(
            "INSERT INTO snapshots (uid, actor, artifact_uri, purpose, hash_kind, content_hash, "
            "taken_at, expires_at, last_seen_hash, last_checked_at, diverged_at, delivered_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT (uid) DO NOTHING",
            (r["uid"], r["actor"], r["artifact_uri"], r["purpose"], r["hash_kind"], r["content_hash"],
             r["taken_at"], r["expires_at"], r["last_seen_hash"], r["last_checked_at"],
             r["diverged_at"], r["delivered_at"]),
        )
        if cur.rowcount and cur.rowcount > 0:
            inserted += 1
    return {"source_rows": len(rows), "inserted": inserted}


def copy_calls(src, dst) -> dict:
    dst.execute(_CALLS_DEDUPE_INDEX)
    rows = src.execute("SELECT * FROM calls ORDER BY id").fetchall()
    inserted = 0
    for r in rows:
        cur = dst.execute(
            "INSERT INTO calls (id, verb, actor, status, code, latency_ms, at, agent_label, via) "
            "OVERRIDING SYSTEM VALUE VALUES (?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT (verb, actor, status, COALESCE(code, ''), latency_ms, at, "
            "COALESCE(agent_label, ''), COALESCE(via, '')) DO NOTHING",
            (r["id"], r["verb"], r["actor"], r["status"], r["code"], r["latency_ms"], r["at"],
             r["agent_label"], r["via"]),
        )
        if cur.rowcount and cur.rowcount > 0:
            inserted += 1
    _bump_identity(dst, "calls", "id")
    return {"source_rows": len(rows), "inserted": inserted}


def copy_conflict_actions(src, dst) -> dict:
    dst.execute(_CONFLICT_ACTIONS_DEDUPE_INDEX)
    rows = src.execute("SELECT * FROM conflict_actions ORDER BY id").fetchall()
    inserted = 0
    for r in rows:
        cur = dst.execute(
            "INSERT INTO conflict_actions (id, lease_uid, with_holder, fired_at, acted_at, action) "
            "OVERRIDING SYSTEM VALUE VALUES (?,?,?,?,?,?) "
            "ON CONFLICT (lease_uid, with_holder, fired_at) DO NOTHING",
            (r["id"], r["lease_uid"], r["with_holder"], r["fired_at"], r["acted_at"], r["action"]),
        )
        if cur.rowcount and cur.rowcount > 0:
            inserted += 1
    _bump_identity(dst, "conflict_actions", "id")
    return {"source_rows": len(rows), "inserted": inserted}


_WORK_KNOWN_FIELDS = {f.name for f in fields(WorkItem)}
_ASOP_KNOWN_FIELDS = {f.name for f in fields(ASOP)}


def copy_work_items(work_jsonl_path: Path, dst) -> dict:
    """Every modelled row in `work.jsonl`, in file order (JSONL's own
    append order — the closest approximation this tool has to the
    original's creation order; there is no `rowid` to inherit, and
    `work_items.ordinal` was never a concept in the SQLite source at all —
    see migration 0008's docstring). Quarantined lines (unreadable by this
    build) are counted and reported, never silently dropped nor migrated —
    same tolerance boundary `SqlQueue._mutate`/`_read_all` already hold.
    """
    queue = Queue(work_jsonl_path)
    raw_rows, _ = queue._read_raw()
    inserted = 0
    for raw in raw_rows:
        item = WorkItem.from_json(json.dumps(raw))
        row = _item_to_row(item)
        unknown = json.dumps({k: v for k, v in raw.items() if k not in _WORK_KNOWN_FIELDS})
        columns = WORK_COLUMNS + ("unknown",)
        placeholders = ", ".join("?" for _ in columns)
        cur = dst.execute(
            f"INSERT INTO work_items ({', '.join(columns)}) VALUES ({placeholders}) "
            f"ON CONFLICT (id) DO NOTHING",
            tuple(row[c] for c in WORK_COLUMNS) + (unknown,),
        )
        if cur.rowcount and cur.rowcount > 0:
            inserted += 1
    return {"source_rows": len(raw_rows), "quarantined": len(queue.quarantined), "inserted": inserted}


def copy_sops(sop_jsonl_path: Path, dst) -> dict:
    """Every modelled version in `sops.jsonl`, into `asops`.

    The destination is the v3 table, and the source may hold either
    generation: `SopLibrary._read_all` upgrades a legacy v2 line to a one-step
    ASOP on the way out (`agentco.sop.upgrade_legacy`), so a file written
    before v3 lands in Postgres already migrated rather than needing a second
    pass afterwards.

    `unknown` is not carried forward by this path — the JSONL reader hands
    back typed `ASOP` objects, not the raw dict a per-version `unknown` would
    have to be read from, and adding that second read path is not worth it for
    a field nothing in the current schema writes yet. Quarantined lines are
    counted and reported.
    """
    library = SopLibrary(sop_jsonl_path)
    asops = library._read_all()
    inserted = 0
    columns = ASOP_COLUMNS + ("unknown",)
    quoted = ", ".join(f'"{c}"' for c in columns)
    placeholders = ", ".join("?" for _ in columns)
    for asop in asops:
        values = _asop_to_row(asop) + ("{}",)
        cur = dst.execute(
            f"INSERT INTO asops ({quoted}) VALUES ({placeholders}) "
            f"ON CONFLICT (asop_id, version) DO NOTHING",
            values,
        )
        if cur.rowcount and cur.rowcount > 0:
            inserted += 1
    return {"source_rows": len(asops), "quarantined": len(library.quarantined), "inserted": inserted}


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--sqlite-db", required=True, help="path to a COPY of registry.sqlite3 — never the live file")
    parser.add_argument("--work-jsonl", required=True, help="path to a COPY of work.jsonl")
    parser.add_argument("--sop-jsonl", required=True, help="path to a COPY of sops.jsonl")
    parser.add_argument("--pg-dsn", required=True, help="postgresql://... target database")
    parser.add_argument("--json", action="store_true", help="machine-readable report")
    args = parser.parse_args(argv)

    if not is_postgres_target(args.pg_dsn):
        print(f"--pg-dsn must be a postgresql:// or postgres:// URL, got {args.pg_dsn!r}", file=sys.stderr)
        return 2
    sqlite_path = Path(args.sqlite_db)
    if not sqlite_path.exists():
        print(f"no such file: {sqlite_path} (copy the source first — never point this at the live file)",
              file=sys.stderr)
        return 2

    src = db.connect(str(sqlite_path))  # migrates the COPY in place if it predates a later migration
    dst = db.connect(args.pg_dsn)       # creates the schema (agentco.migrations) if the target is fresh

    report: dict[str, Any] = {}
    try:
        report["events"] = copy_events(src, dst)
        report["leases"] = copy_leases(src, dst)
        report["snapshots"] = copy_snapshots(src, dst)
        report["calls"] = copy_calls(src, dst)
        report["conflict_actions"] = copy_conflict_actions(src, dst)
        report["work_items"] = copy_work_items(Path(args.work_jsonl), dst)
        report["sops"] = copy_sops(Path(args.sop_jsonl), dst)
    finally:
        src.close()
        dst.close()

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        for table, stats in report.items():
            print(f"{table}: {stats}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
