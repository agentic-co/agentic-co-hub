"""Storage-layer defects R16-R21 from the 2026-11-07 review, each as the property that SHOULD hold.

Conventions are `tests/test_adversarial_findings.py`'s: every test began life
FAILING against the code and marked `xfail(strict=True)`, the name states the
property rather than the bug, and the marker comes off in the same change as
the fix. Rows live in `docs/known-issues.md` under "Open, from the 2026-11-07
review (R4-R21)".

**The Postgres tests run against a real server** (`AGENTCO_TEST_PG`) and are
skipped without one, for the reason `conftest` gives: these are concurrency
defects in how Postgres orders commits and takes locks, and reading the code
was how they were found, not how they are proved. Each uses `conftest`'s
`_pg_dsn` for a fresh schema, and each opens SEPARATE store objects per
contender — one store object serialises its own callers on an in-process lock,
so contenders sharing one would prove nothing about two processes.

Every thread here joins with a timeout and every wait is bounded: a fix that
serialises writers must make a test slower, never hang it.
"""

from __future__ import annotations

import base64
import os
import threading
import time

import pytest

from agentco import db, events, migrations
from agentco.clearance import Clearance
from agentco.errors import Refusal
from agentco.memory import MemoryStore, SqlMemoryStore
from agentco.sqlstore import SqlQueue, SqlSopLibrary
from agentco.work import DecompositionError, MAX_CHILDREN, WorkError

PG = os.environ.get("AGENTCO_TEST_PG")
needs_pg = pytest.mark.skipif(not PG, reason="AGENTCO_TEST_PG is not set")

JOIN_S = 60


def _contend(n, target):
    """Run `target(i)` on n threads released together. Returns (results, errors)."""
    gate = threading.Barrier(n)
    results, errors = [None] * n, [None] * n

    def body(i):
        try:
            gate.wait(timeout=10)
            results[i] = target(i)
        except BaseException as exc:  # noqa: BLE001 - every outcome is asserted on
            errors[i] = exc

    threads = [threading.Thread(target=body, args=(i,), daemon=True) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=JOIN_S)
    assert not any(t.is_alive() for t in threads), "a contender hung"
    return results, errors


# --------------------------------------------------------------------------- #
# R16 — the change feed must not lose an event that commits late
# --------------------------------------------------------------------------- #


@pytest.mark.xfail(strict=True, reason="R16: seq is assigned at INSERT and visible at COMMIT; nothing orders commits on Postgres")
@needs_pg
def test_a_feed_reader_never_skips_an_event_that_commits_after_a_later_seq(_pg_dsn):
    """T1 takes seq N and holds its transaction open; T2 takes N+1 and commits;
    a reader reads in between, then T1 commits. The reader must still get N.

    `seq` is assigned at INSERT and visible at COMMIT, so without anything
    ordering the two, the reader sees N+1, advances its cursor past N, and N is
    gone for good. Any honest fix is accepted: T2 waiting for T1 (commit order
    == seq order), or the reader declining to advance past an in-flight seq.
    T2 runs on its own thread with a bounded wait because the first fix makes it
    BLOCK until T1 commits — that is the fix working, not a hang.
    """
    conn = db.connect(_pg_dsn)
    try:
        inserted, release = threading.Event(), threading.Event()
        held: dict = {}

        def slow_writer():
            with conn:
                held["seq"] = events.insert(conn, kind="ScopeClaimed", actor="t1", payload={"n": 1})["seq"]
                inserted.set()
                release.wait(timeout=JOIN_S)

        t1 = threading.Thread(target=slow_writer, daemon=True)
        t1.start()
        assert inserted.wait(timeout=10)

        fast: dict = {}
        t2 = threading.Thread(
            target=lambda: fast.update(events.append(conn, kind="ScopeClaimed", actor="t2", payload={"n": 2})),
            daemon=True)
        t2.start()
        t2.join(timeout=1.5)  # committed by now, unless something makes it wait for T1

        seen = []
        first = events.read(conn, since=events.encode_cursor(0), limit=500)
        seen += [e["seq"] for e in first["events"]]
        release.set()
        t1.join(timeout=JOIN_S)
        t2.join(timeout=JOIN_S)
        assert not t1.is_alive() and not t2.is_alive()
        second = events.read(conn, since=first["nextCursor"], limit=500)
        seen += [e["seq"] for e in second["events"]]

        assert held["seq"] in seen, (
            f"seq {held['seq']} committed after the reader advanced to {first['nextCursor']!r} "
            f"and was never delivered; the reader saw {seen}")
        assert fast["seq"] in seen
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# R17 — concurrent cold starts both succeed
# --------------------------------------------------------------------------- #


@pytest.mark.xfail(strict=True, reason="R17: BEGIN IMMEDIATE is a plain BEGIN on Postgres; concurrent migrators collide")
@needs_pg
def test_concurrent_cold_starts_against_one_fresh_postgres_schema_both_succeed(_pg_dsn):
    """Four processes' worth of connections opening one EMPTY schema together.
    Every one must come up, and each migration must be applied exactly once
    across all of them — the ledger is what says which happened."""
    from agentco import pgadapter

    conns = [pgadapter.connect(_pg_dsn) for _ in range(4)]
    try:
        results, errors = _contend(len(conns), lambda i: migrations.apply(conns[i]))
        assert errors == [None] * len(conns), errors
        applied = sorted(v for r in results for v in (r or []))
        assert applied == sorted(m.version for m in migrations.MIGRATIONS), (
            "every migration applied exactly once across the contenders")
        assert migrations.applied_versions(conns[0]) == {m.version for m in migrations.MIGRATIONS}
    finally:
        for c in conns:
            c.close()


# --------------------------------------------------------------------------- #
# R18 — concurrent child creates under one parent keep the bound and the block
# --------------------------------------------------------------------------- #


@pytest.mark.xfail(strict=True, reason="R18: the parent's read-modify-write is unlocked on Postgres")
@needs_pg
def test_concurrent_child_creates_under_one_parent_keep_the_cap_and_every_block(_pg_dsn):
    """Several writers filing children of one goal at once. The cap must hold,
    and every child that was created must be in the parent's `blocked_by` — a
    lost entry is a parent that can close with that child still open."""
    parent = SqlQueue(_pg_dsn).create("the goal")
    n = MAX_CHILDREN + 3
    stores = [SqlQueue(_pg_dsn) for _ in range(n)]
    results, errors = _contend(n, lambda i: stores[i].create(f"unit {i}", metadata={"parent": parent.id}))

    unexpected = [e for e in errors if e is not None and not isinstance(e, (DecompositionError, WorkError))]
    assert not unexpected, unexpected
    created = [r.id for r in results if r is not None]
    assert len(created) <= MAX_CHILDREN, f"{len(created)} children under a cap of {MAX_CHILDREN}"
    blocked = set(SqlQueue(_pg_dsn).get(parent.id).blocked_by)
    assert set(created) <= blocked, f"children missing from the parent's blocked_by: {set(created) - blocked}"


# --------------------------------------------------------------------------- #
# R19 — concurrent SOP authoring never destroys or reissues a version
# --------------------------------------------------------------------------- #


@pytest.mark.xfail(strict=True, reason="R19: SqlSopLibrary._write_all deletes and re-inserts the whole table")
@needs_pg
def test_concurrent_revisions_of_one_sop_get_distinct_versions_and_lose_none(_pg_dsn):
    from tests.test_sop import feature_dev_body

    first = SqlSopLibrary(_pg_dsn).create("a feature, end to end", **feature_dev_body())
    n = 6
    libs = [SqlSopLibrary(_pg_dsn) for _ in range(n)]
    results, errors = _contend(n, lambda i: libs[i].revise(
        first.asop_id, purpose=f"revision {i}", author="carol", author_kind="human"))

    assert errors == [None] * n, errors
    versions = sorted(r.version for r in results)
    assert versions == list(range(2, n + 2)), f"versions issued: {versions}"
    history = SqlSopLibrary(_pg_dsn).history(first.asop_id)
    assert sorted(a.version for a in history) == list(range(1, n + 2)), "a committed version was lost"
    by_version = {a.version: a.purpose for a in history}
    assert {r.version: r.purpose for r in results}.items() <= by_version.items(), (
        "a version number now names different text than the revision that was issued it")


@pytest.mark.xfail(strict=True, reason="R19: SqlSopLibrary._write_all deletes and re-inserts the whole table")
@needs_pg
def test_concurrent_creates_never_delete_a_committed_sop(_pg_dsn):
    """Creates of DIFFERENT SOPs racing while one already exists. Every
    committed SOP must survive every other create."""
    from tests.test_sop import feature_dev_body

    seeded = SqlSopLibrary(_pg_dsn).create("already here", **feature_dev_body())
    n = 6
    libs = [SqlSopLibrary(_pg_dsn) for _ in range(n)]
    results, errors = _contend(n, lambda i: libs[i].create(f"procedure {i}", **feature_dev_body()))
    assert errors == [None] * n, errors
    stored = {a.asop_id for a in SqlSopLibrary(_pg_dsn)._read_all()}
    expected = {r.asop_id for r in results} | {seeded.asop_id}
    assert expected <= stored, f"committed SOPs deleted by a concurrent create: {expected - stored}"


@pytest.mark.xfail(strict=True, reason="R19: no row to lock on an empty table; delete-all destroys the other create")
@needs_pg
def test_a_first_create_on_an_empty_sop_table_survives_a_concurrent_one(_pg_dsn):
    """The case no row lock can cover: the table is empty, so there is nothing
    to lock. The interleaving is forced rather than raced for: B reads the
    (empty) history while A's write is still uncommitted, then A commits, then
    B writes. Every wait is bounded — a fix that makes B wait for A turns the
    forced interleaving into a short stall, not a hang."""
    from tests.test_sop import feature_dev_body

    a, b = SqlSopLibrary(_pg_dsn), SqlSopLibrary(_pg_dsn)
    b_read, a_committed = threading.Event(), threading.Event()
    real_a_write, real_b_read = a._write_all, b._read_all

    def a_write(*args, **kwargs):
        real_a_write(*args, **kwargs)
        b_read.wait(timeout=2)

    def b_reads(*args, **kwargs):
        out = real_b_read(*args, **kwargs)
        if not b_read.is_set():  # the authoring read, the only one create makes
            b_read.set()
            a_committed.wait(timeout=10)
        return out

    a._write_all, b._read_all = a_write, b_reads
    made: dict = {}
    ta = threading.Thread(target=lambda: made.update(a=a.create("first", **feature_dev_body())), daemon=True)
    ta.start()
    time.sleep(0.2)  # A is inside its transaction, write done, waiting for B's read
    tb = threading.Thread(target=lambda: made.update(b=b.create("second", **feature_dev_body())), daemon=True)
    tb.start()
    ta.join(timeout=JOIN_S)
    a_committed.set()
    tb.join(timeout=JOIN_S)
    assert not ta.is_alive() and not tb.is_alive()
    stored = {s.asop_id for s in SqlSopLibrary(_pg_dsn)._read_all()}
    assert {made["a"].asop_id, made["b"].asop_id} <= stored, f"stored: {stored}"


# --------------------------------------------------------------------------- #
# R20 — a redelivered trace is credited once, even after a crash mid-record
# --------------------------------------------------------------------------- #

LEVELS = ("all", "internal", "confidential", "private")
ACME = Clearance({"acme": "confidential"}, LEVELS)
PROV = {"kind": "run", "ref": "run:w-0001"}


@pytest.fixture()
def memory(backend, tmp_path, request):
    if backend == "jsonl":
        return MemoryStore(tmp_path / "memory.jsonl", hub_id="hub-a")
    if backend == "postgres":
        return SqlMemoryStore(request.getfixturevalue("_pg_dsn"), hub_id="hub-a")
    return SqlMemoryStore(tmp_path / "agentco.sqlite3", hub_id="hub-a")


@pytest.mark.parametrize("fails", ["_put_states", "_add_credits"])
def test_a_trace_interrupted_between_its_two_writes_is_never_credited_twice(memory, backend, fails, request):
    """Fault injection between the state write and the dedupe marker, in either
    order. The trace is then redelivered — the at-least-once transport's normal
    behaviour after a failed call — and must not count twice.

    Either honest fix passes: the marker written first (a crash then loses the
    one trace, never doubles it), or a state write that is itself idempotent on
    the trace id. The SQL stores write both in one transaction and hold this.
    """
    if backend == "jsonl" and fails == "_add_credits":
        request.applymarker(pytest.mark.xfail(strict=True, reason=(
            "R20: JSONL record_trace writes states, then the credits ledger that _trace_seen reads")))
    m = memory.create(actor="kofi", clearance=ACME, owner="acme", level="internal", layer="shared",
                      title="Retry the export once", body="A stalled export resumes after one retry.",
                      provenance=PROV)

    def boom(*_a, **_k):
        raise RuntimeError("injected fault")

    setattr(memory, fails, boom)
    with pytest.raises(RuntimeError, match="injected fault"):
        memory.record_trace(actor="kofi", clearance=ACME, trace_id="t1", outcome="success",
                            invoked=[m.item_id], retrieved=[m.item_id])
    delattr(memory, fails)

    memory.record_trace(actor="kofi", clearance=ACME, trace_id="t1", outcome="success",
                        invoked=[m.item_id], retrieved=[m.item_id])
    state = memory.state(m.item_id, ACME)
    assert state["state"]["successes"] <= 1, f"one trace credited {state['state']['successes']} times"
    assert len([c for c in state["credits"] if c["trace_id"] == "t1"]) <= 1


# --------------------------------------------------------------------------- #
# R21 — cursor edges
# --------------------------------------------------------------------------- #


@pytest.fixture()
def conn(backend, tmp_path, request):
    if backend == "jsonl":
        pytest.skip("the event feed has no JSONL backend")
    if backend == "postgres":
        c = db.connect(request.getfixturevalue("_pg_dsn"))
    else:
        c = db.connect(tmp_path / "registry.sqlite3")
    yield c
    c.close()


def _forged(position: str) -> str:
    return base64.urlsafe_b64encode(f"{events.CURSOR_PREFIX}{position}".encode()).decode().rstrip("=")


@pytest.mark.xfail(strict=True, reason="R21: an out-of-range seq reaches the driver (OverflowError) or the floor check (410)")
@pytest.mark.parametrize("position", ["99999999999999999999", str(2**63), "-1"])
def test_a_cursor_outside_the_seq_range_is_refused_as_a_bad_cursor(conn, position):
    """Refused at the layer that can name it (400 `bad_cursor`), never an
    uncaught `OverflowError` from the driver (a 500) and never a 410 that tells
    an honest client its well-formed cursor expired."""
    events.append(conn, kind="ScopeClaimed", actor="a", payload={})
    with pytest.raises(Refusal) as refused:
        events.read(conn, since=_forged(position))
    assert refused.value.code == "bad_cursor" and refused.value.http_status == 400


@pytest.mark.xfail(strict=True, reason="R21: the floor check reads since='' as a cursor at seq 0")
def test_an_empty_cursor_after_a_prune_starts_at_the_floor_like_no_cursor(conn):
    """`decode_cursor` reads `since=""` as no cursor; the floor check must too."""
    old, new = "2026-01-01T00:00:00+00:00", "2026-10-18T00:00:00+00:00"
    for _ in range(3):
        events.append(conn, kind="ScopeClaimed", actor="a", payload={}, occurred_at=old)
    kept = [events.append(conn, kind="ScopeClaimed", actor="a", payload={}, occurred_at=new)["seq"]
            for _ in range(2)]
    events.prune(conn, before="2026-06-01T00:00:00+00:00")
    assert [e["seq"] for e in events.read(conn, since="")["events"]] == kept
    assert events.read(conn, since="") == events.read(conn)

