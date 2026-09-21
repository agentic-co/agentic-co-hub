"""The Postgres backend serves callers in parallel, and bounds what it takes.

Every test here is about the property the old adapter could not have: it held
ONE connection behind a process-wide `threading.RLock`, so two callers could
never be inside the database at the same instant no matter how idle the server
was. The suite passed against that design — a lock does not break correctness,
it breaks throughput — which is why these assertions are about TIME and about
distinct backends rather than about results.

Skipped entirely without `AGENTCO_TEST_PG`, for the reason `conftest` gives
about the postgres backend generally: a test that silently passes when the
thing it tests is absent is worse than one that is not there.
"""

from __future__ import annotations

import os
import threading
import time

import pytest

from agentco import pgadapter

DSN = os.environ.get("AGENTCO_TEST_PG")
pytestmark = pytest.mark.skipif(not DSN, reason="AGENTCO_TEST_PG is not set")

SLEEP_S = 0.25
THREADS = 8


@pytest.fixture()
def conn():
    c = pgadapter.connect(DSN)
    yield c
    c.close()


def _run(target, n=THREADS):
    """Start n threads, release them together, return (wall_seconds, results)."""
    start = threading.Barrier(n)
    out, errors = [], []

    def body(i):
        try:
            start.wait(timeout=10)
            out.append(target(i))
        except Exception as exc:  # noqa: BLE001 - surfaced by the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=body, args=(i,)) for i in range(n)]
    t0 = time.monotonic()
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    elapsed = time.monotonic() - t0
    assert not errors, errors
    return elapsed, out


def test_statements_from_different_threads_do_not_queue_behind_each_other(conn):
    """Eight sleeps of a quarter second. Serialised that is two seconds; in
    parallel it is a quarter plus overhead. The old adapter could only produce
    the first number, which is what makes this a regression test and not a
    benchmark."""
    elapsed, _ = _run(lambda i: conn.execute(f"SELECT pg_sleep({SLEEP_S})").fetchall())

    serial = SLEEP_S * THREADS
    assert elapsed < serial / 2, (
        f"{THREADS} concurrent statements took {elapsed:.2f}s; serialised would be "
        f"{serial:.2f}s, so this is queueing rather than running in parallel"
    )


def test_concurrent_callers_are_on_different_server_connections(conn):
    """`pg_backend_pid()` is the server's own answer to "is this the same
    connection", so it cannot be satisfied by an adapter that merely looks
    concurrent."""
    _, pids = _run(lambda i: conn.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"])
    assert len(set(pids)) > 1, f"every caller landed on one backend: {set(pids)}"


def test_a_transaction_keeps_one_connection_for_its_whole_span(conn):
    """The reason a pool cannot simply borrow per statement: a transaction that
    changed connections half way through would commit one half and leave the
    other on a connection nobody holds."""
    with conn:
        first = conn.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"]
        conn.execute("SELECT 1").fetchall()
        second = conn.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"]
    assert first == second


def test_two_threads_in_transactions_hold_two_different_connections(conn):
    def in_tx(i):
        with conn:
            pid = conn.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"]
            time.sleep(0.2)          # overlap, so both are open at once
            return pid

    _, pids = _run(in_tx, n=2)
    assert len(set(pids)) == 2, f"both transactions shared a connection: {pids}"


def test_in_transaction_is_answered_per_thread(conn):
    """A process-wide answer would be true for a caller that is not in one."""
    seen = {}
    ready, done = threading.Event(), threading.Event()

    def holder():
        with conn:
            seen["inside"] = conn.in_transaction
            ready.set()
            done.wait(timeout=10)

    t = threading.Thread(target=holder)
    t.start()
    assert ready.wait(timeout=10)
    seen["other_thread"] = conn.in_transaction   # this thread is NOT in one
    done.set()
    t.join(timeout=10)

    assert seen["inside"] is True
    assert seen["other_thread"] is False


def test_a_failed_transaction_gives_its_connection_back(conn):
    """A connection kept out of the pool by a failure is one the pool never
    gets back, and enough of those is an outage that presents as a hang."""
    before = conn._pool.get_stats().get("pool_size", 0)
    for _ in range(5):
        with pytest.raises(Exception):
            with conn:
                conn.execute("SELECT 1").fetchall()
                conn.execute("SELECT * FROM a_table_that_does_not_exist").fetchall()
    # still usable, and not leaking a connection per failure
    assert conn.execute("SELECT 1 AS n").fetchone()["n"] == 1
    assert conn._pool.get_stats().get("pool_available", 0) >= 1
    assert conn._pool.get_stats().get("pool_size", 0) <= max(before, 1) + pgadapter.DEFAULT_POOL_MAX


def test_the_operator_can_bound_what_this_process_takes(monkeypatch):
    """Pointed at a SHARED server, a process that opens a connection per thread
    is a process that can exhaust a database other applications are using."""
    monkeypatch.setenv(pgadapter.POOL_MAX_ENV_VAR, "3")
    monkeypatch.setenv(pgadapter.POOL_MIN_ENV_VAR, "1")
    c = pgadapter.connect(DSN)
    try:
        assert c._max == 3
        _, pids = _run(lambda i: c.execute(f"SELECT pg_sleep({SLEEP_S}), pg_backend_pid() AS pid").fetchone()["pid"])
        assert len(set(pids)) <= 3, f"pool exceeded its bound: {set(pids)}"
    finally:
        c.close()


def test_a_nonsense_bound_does_not_stop_the_registry_starting(monkeypatch):
    """A registry that refuses to boot over a pool size is a worse outcome than
    one that boots with a sensible number."""
    monkeypatch.setenv(pgadapter.POOL_MAX_ENV_VAR, "not-a-number")
    monkeypatch.setenv(pgadapter.POOL_MIN_ENV_VAR, "99")
    assert pgadapter._pool_bounds() == (pgadapter.DEFAULT_POOL_MAX, pgadapter.DEFAULT_POOL_MAX)
