"""What the SQLite backend must hold that the JSONL one cannot be asked about.

The conformance suite (`conftest.py`) proves both backends satisfy the same
behavioural contract. These are the properties that only exist once storage is
a database: that a reopened file still holds the work, that the fenced CAS is
a real transaction rather than a read followed by a write, that a cursor
handed out before a restart still resumes after it, and that running the
migrations twice is not the same as running them twice as many times.

The concurrency tests spawn real OS processes for the same reason the JSONL
ones do: a lock that is only ever exercised by one process is a lock nothing
has tested.
"""

from __future__ import annotations

import json
import multiprocessing as mp
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from agentco import db, events, migrations
from agentco.sop import SopLibrary, SopStatus
from agentco.sqlstore import SqlQueue, SqlSopLibrary
from agentco.stores import AGENTCO_DB_ENV_VAR, open_queue, open_sop_library
from agentco.work import CapabilityError, LeaseError, Queue, WorkStatus

NOW = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# Durability — the whole point
# --------------------------------------------------------------------------- #


def test_work_survives_reopening_the_database(tmp_path):
    path = tmp_path / "agentco.sqlite3"
    first = SqlQueue(path)
    item = first.create("survive a restart", requires=["gpu"], metadata={"a": 1})
    first.claim(item.id, "worker-a", capabilities=["gpu"], now=NOW)
    first.close()

    reopened = SqlQueue(path)
    found = reopened.get(item.id)
    assert found is not None
    assert found.title == "survive a restart"
    assert found.status == WorkStatus.IN_PROGRESS
    assert found.leased_by == "worker-a"
    assert found.lease_attempt == 1
    assert found.requires == ["gpu"]
    assert found.metadata["a"] == 1


def test_sops_survive_reopening_the_database(tmp_path):
    path = tmp_path / "agentco.sqlite3"
    first = SqlSopLibrary(path)
    sop = first.create("weekly close", purpose="shut the books", common_mistakes=["skip the reconcile"])
    first.revise(sop.sop_id, purpose="shut the books, properly")
    first.activate(sop.sop_id, 2)
    first.close()

    reopened = SqlSopLibrary(path)
    history = reopened.history(sop.sop_id)
    assert [s.version for s in history] == [1, 2]
    assert history[0].status is SopStatus.SUPERSEDED or history[0].superseded_by == 2
    active = reopened.get(sop.sop_id)
    assert active is not None and active.version == 2
    # Carry-forward survived the round trip through columns, not just memory.
    assert active.common_mistakes == ["skip the reconcile"]


def test_a_cursor_handed_out_before_a_restart_still_resumes_after_it(tmp_path):
    """The feed's contract is that the cursor is resumable. A cursor that only
    resumes within one process lifetime is a windowed query wearing a cursor's
    name."""
    path = tmp_path / "agentco.sqlite3"
    conn = db.connect(path)
    events.append(conn, kind="ScopeClaimed", actor="a", repo="r", payload={"n": 1})
    events.append(conn, kind="ScopeClaimed", actor="a", repo="r", payload={"n": 2})
    page = events.read(conn, limit=1)
    cursor = page["nextCursor"]
    conn.close()

    reopened = db.connect(path)
    events.append(reopened, kind="ScopeReleased", actor="a", repo="r", payload={"n": 3})
    resumed = events.read(reopened, since=cursor)
    assert [e["payload"]["n"] for e in resumed["events"]] == [2, 3]


# --------------------------------------------------------------------------- #
# The CAS is a transaction, not a read followed by a write
# --------------------------------------------------------------------------- #


def _claim_worker(db_path: str, item_id: str, agent: str, result_path: str, barrier) -> None:
    queue = SqlQueue(db_path)
    barrier.wait(timeout=30)
    try:
        won = queue.claim(item_id, agent, now=NOW) is not None
    except Exception as exc:  # pragma: no cover - a failure here is the finding
        won = False
        result_path = result_path
        with open(result_path, "a") as handle:
            handle.write(json.dumps({"agent": agent, "error": repr(exc)}) + "\n")
        return
    with open(result_path, "a") as handle:
        handle.write(json.dumps({"agent": agent, "won": won}) + "\n")


def test_two_clients_race_one_claim_and_exactly_one_wins(tmp_path):
    """Twelve real processes, barrier-synced. If the CAS were a read followed
    by a write, more than one would come back holding the same item."""
    path = tmp_path / "agentco.sqlite3"
    queue = SqlQueue(path)
    item = queue.create("contended")
    queue.close()

    results = tmp_path / "results.jsonl"
    results.write_text("")
    ctx = mp.get_context("spawn")
    barrier = ctx.Barrier(12)
    procs = [
        ctx.Process(
            target=_claim_worker,
            args=(str(path), item.id, f"worker-{i}", str(results), barrier),
        )
        for i in range(12)
    ]
    for proc in procs:
        proc.start()
    for proc in procs:
        proc.join(timeout=60)
        assert proc.exitcode == 0, f"worker exited {proc.exitcode}"

    rows = [json.loads(line) for line in results.read_text().splitlines() if line.strip()]
    assert len(rows) == 12, rows
    assert not [r for r in rows if "error" in r], rows
    winners = [r["agent"] for r in rows if r["won"]]
    assert len(winners) == 1, f"expected exactly one winner, got {winners}"

    final = SqlQueue(path).get(item.id)
    assert final is not None
    assert final.leased_by == winners[0]
    assert final.lease_attempt == 1, "one claim must bump the fence exactly once"


def test_a_refused_claim_leaves_the_row_untouched(tmp_path):
    """The JSONL backend proves this by comparing file bytes; the database's
    equivalent is that nothing about the row moved — including `updated_at`,
    which a read-then-write implementation would bump on the way out."""
    queue = SqlQueue(tmp_path / "agentco.sqlite3")
    item = queue.create("build", requires=["gpu"])
    before = queue._row(item.id)
    with pytest.raises(CapabilityError):
        queue.claim(item.id, "worker-a", capabilities=["cpu"], now=NOW)
    assert queue._row(item.id) == before


def test_a_stale_report_is_fenced_out_and_writes_nothing(tmp_path):
    queue = SqlQueue(tmp_path / "agentco.sqlite3")
    item = queue.create("x")
    queue.claim(item.id, "worker-a", now=NOW)
    # The lease lapses and someone else takes it.
    later = NOW + timedelta(hours=2)
    queue.claim(item.id, "worker-b", now=later)
    with pytest.raises(LeaseError):
        queue.report_result(item.id, attempt=1, status=WorkStatus.DONE, result="late")
    after = queue.get(item.id)
    assert after is not None
    assert after.result is None
    assert after.leased_by == "worker-b"


def test_the_unique_natural_key_is_enforced_by_the_database(tmp_path):
    """Not by a scan the application does before inserting. A scan is advisory
    under concurrency; the index is the rule."""
    path = tmp_path / "agentco.sqlite3"
    queue = SqlQueue(path)
    first = queue.create("nightly", kind="nightly", subject="ledger", period="2026-08-31")
    second = queue.create("nightly", kind="nightly", subject="ledger", period="2026-08-31")
    assert second.id == first.id

    with pytest.raises(sqlite3.IntegrityError):
        queue._conn.execute(
            "INSERT INTO work_items "
            "(id,title,status,requires,blocked_by,lease_attempt,natural_key,"
            " metadata,created_at,updated_at,unknown) "
            "VALUES ('w-forced','forced','pending','[]','[]',0,?,'{}','t','t','{}')",
            (first.natural_key,),
        )


def test_a_newer_writers_column_survives_an_update(tmp_path):
    """Same promise the JSONL `_merge` makes: this version must not delete a
    field a newer one wrote. In the database that field lives in `unknown`."""
    queue = SqlQueue(tmp_path / "agentco.sqlite3")
    item = queue.create("x")
    queue._conn.execute(
        "UPDATE work_items SET unknown = ? WHERE id = ?",
        (json.dumps({"future_column": {"added_by": "a newer version"}}), item.id),
    )
    queue.claim(item.id, "worker-a", now=NOW)
    row = queue._row(item.id)
    assert json.loads(row["unknown"])["future_column"] == {"added_by": "a newer version"}
    assert row["leased_by"] == "worker-a"


# --------------------------------------------------------------------------- #
# Migrations
# --------------------------------------------------------------------------- #


def test_migrations_are_applied_once_and_recorded(tmp_path):
    path = tmp_path / "agentco.sqlite3"
    conn = db.connect(path)
    applied = [row["version"] for row in conn.execute(
        "SELECT version FROM schema_migrations ORDER BY version"
    )]
    assert applied == [m.version for m in migrations.MIGRATIONS]


def test_applying_the_migrations_again_is_a_no_op(tmp_path):
    """Idempotency is not "the DDL says IF NOT EXISTS". It is that a second
    run applies nothing and does not restamp what the first run recorded."""
    path = tmp_path / "agentco.sqlite3"
    conn = db.connect(path)
    stamps = {row["version"]: row["applied_at"] for row in conn.execute(
        "SELECT version, applied_at FROM schema_migrations"
    )}
    assert migrations.apply(conn) == []
    again = {row["version"]: row["applied_at"] for row in conn.execute(
        "SELECT version, applied_at FROM schema_migrations"
    )}
    assert again == stamps


def test_a_second_process_opening_the_same_file_applies_nothing(tmp_path):
    path = tmp_path / "agentco.sqlite3"
    db.connect(path)
    second = db.connect(path)
    assert migrations.apply(second) == []


def test_a_database_created_before_the_ledger_existed_is_adopted_not_rebuilt(tmp_path):
    """The registry tables predate the migration ledger, so the first
    migration has to be safe to "apply" to a file that already has them —
    otherwise upgrading an existing deployment means dropping its leases."""
    path = tmp_path / "legacy.sqlite3"
    raw = sqlite3.connect(str(path))
    raw.executescript(";\n".join(migrations.MIGRATIONS[0].statements) + ";")
    raw.execute(
        "INSERT INTO leases (uid, holder, repo, prefixes, intent, claimed_at, expires_at) "
        "VALUES ('l-1','a','r','[\"src/one\"]','implement','t','t')"
    )
    raw.commit()
    raw.close()

    conn = db.connect(path)
    assert conn.execute("SELECT COUNT(*) FROM leases").fetchone()[0] == 1
    applied = [row["version"] for row in conn.execute(
        "SELECT version FROM schema_migrations ORDER BY version"
    )]
    assert applied == [m.version for m in migrations.MIGRATIONS]


# --------------------------------------------------------------------------- #
# Backend selection
# --------------------------------------------------------------------------- #


def test_unset_means_exactly_todays_behaviour(monkeypatch, tmp_path):
    monkeypatch.delenv(AGENTCO_DB_ENV_VAR, raising=False)
    assert isinstance(open_queue(str(tmp_path / "work.jsonl")), Queue)
    assert isinstance(open_sop_library(str(tmp_path / "sops.jsonl")), SopLibrary)


def test_setting_the_env_var_selects_the_database(monkeypatch, tmp_path):
    path = tmp_path / "agentco.sqlite3"
    monkeypatch.setenv(AGENTCO_DB_ENV_VAR, str(path))
    queue = open_queue(str(tmp_path / "ignored.jsonl"))
    library = open_sop_library(str(tmp_path / "ignored-sops.jsonl"))
    assert isinstance(queue, SqlQueue)
    assert isinstance(library, SqlSopLibrary)
    # And they share the one file, which is the deployment shape the env var
    # describes — not two databases that happen to both be SQLite.
    assert queue.path == path == library.path
    assert not (tmp_path / "ignored.jsonl").exists()


def test_an_explicit_db_argument_beats_the_environment(monkeypatch, tmp_path):
    monkeypatch.setenv(AGENTCO_DB_ENV_VAR, str(tmp_path / "from-env.sqlite3"))
    queue = open_queue(db=str(tmp_path / "explicit.sqlite3"))
    assert queue.path == tmp_path / "explicit.sqlite3"


def test_the_two_stores_can_share_one_file_with_the_registry(tmp_path):
    """One SQLite file is the storage design. If the work tables and the
    registry tables could not coexist, `AGENTCO_DB` would be a lie."""
    path = tmp_path / "agentco.sqlite3"
    conn = db.connect(path)
    events.append(conn, kind="SnapshotTaken", actor="a", repo="r", payload={})
    queue = SqlQueue(path)
    library = SqlSopLibrary(path)
    sop = library.create("procedure", purpose="do the thing")
    library.activate(sop.sop_id, 1)
    item = library.instantiate(sop.sop_id, queue)
    assert item.metadata["sop_ref"] == {"sop_id": sop.sop_id, "version": 1}
    assert events.read(conn)["events"]
    assert queue.get(item.id) is not None
