"""Several connectors opening the same brand-new registry file at once.

The WAL conversion of a fresh file takes a lock SQLite does not route through
the busy handler, so without care the second opener raised
`database is locked`.
"""

from __future__ import annotations

import threading

from agentco import db

THREADS = 8
ROUNDS = 20


def test_concurrent_first_open_of_a_fresh_database_always_succeeds(tmp_path):
    for round_no in range(ROUNDS):
        path = tmp_path / f"fresh-{round_no}.sqlite3"
        barrier = threading.Barrier(THREADS)
        errors: list[BaseException] = []
        conns = []

        def opener():
            try:
                barrier.wait(timeout=30)
                conns.append(db.connect(path))
            except BaseException as exc:  # noqa: BLE001 - asserted on below
                errors.append(exc)

        threads = [threading.Thread(target=opener, daemon=True) for _ in range(THREADS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        assert not any(t.is_alive() for t in threads), "an opener hung"
        assert not errors, errors
        assert len(conns) == THREADS
        for c in conns:
            assert c.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
            c.close()
