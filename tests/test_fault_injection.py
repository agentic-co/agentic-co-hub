"""Fault injection: the hub stays correct when a write fails or a process dies
at the worst moment, on every backend.

Most recorded defects here are protocol bugs around atomicity and recovery, not
logic errors, and ordinary tests cannot reach them: they call a store, the store
answers, nothing is ever interrupted between two writes. These tests interrupt.

Two techniques, and the split matters:

  1. **In-process injection.** An internal step is made to raise between two
     writes. The store must then be wholly BEFORE or wholly AFTER the operation
     (never half), and an honest retry must converge. These run on every backend
     the `backend` fixture yields.
  2. **Real process death.** A subprocess is killed (SIGKILL) in the middle of a
     write. Nothing a `finally` block does can help it, which is the point. The
     store is then reopened in the test process. JSONL and SQLite only: a
     Postgres server's crash recovery is the server's, not this code's.

`tests/test_envelope.py` and `tests/test_humankeys.py` already carry crash tests
for their own stores, and `tests/test_outbox_concurrency.py` kills a process
mid-line and drains. This file covers the stores they do not and does not repeat
them.

## Defects this found

A test marked `xfail(strict=True)` names a defect the injection exposed. The
convention is `tests/test_adversarial_findings.py`'s: the test states the
property that SHOULD hold, so the suite stays green while the defect stands and
turns red the moment it is fixed with the marker left in. Each has a row in
`docs/known-issues.md`. Product code is deliberately untouched by this file.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from agentco import db, events
from agentco.clearance import Clearance
from agentco.memory import MemoryStore, SqlMemoryStore
from agentco.outbox import Outbox, drain
from agentco.sqlstore import SqlQueue
from agentco.work import Queue, WorkStatus


# --------------------------------------------------------------------------- #
# shared helpers
# --------------------------------------------------------------------------- #

PROCESS_TIMEOUT_S = 30


def boom(*_args, **_kwargs):
    raise RuntimeError("injected fault")


def rows_of(queue) -> list[dict]:
    """Every stored row, as the backend hands it around. Backend-neutral."""
    return queue._read_raw()[0]


GATE = {"kind": "deterministic", "check": "pytest -q", "max_park_seconds": 900, "on_timeout": "fail"}
ATTESTATION = {"check": "pytest -q", "exit_status": 0, "environment": "test",
               "at": "2026-09-04T00:00:00+00:00"}


def a_run(library, queue):
    """A run container with two gated steps, filed the way the plane files one."""
    step = {"role": "implementer", "purpose": "do it", "definition_of_done": "done", "gate": GATE}
    asop = library.create(
        "fault run", task_type="feature", purpose="p", trigger="t",
        inputs=[{"name": "requirement", "description": "r"}],
        roles={"implementer": {"kind": "agent"}},
        steps=[{"name": "one", **step}, {"name": "two", **step}],
    )
    library.activate(asop.asop_id, asop.version)
    return library.run(asop.asop_id, queue, inputs={"requirement": "REQ-1"},
                       bindings={"implementer": "alice"})


def finish(queue, item_id, *, key=None):
    queue.claim(item_id, "alice", capabilities=["verify"])
    item = queue.get(item_id)
    return queue.report_result(item_id, item.lease_attempt, WorkStatus.DONE, result="did it",
                               attestation=ATTESTATION, submitted_by="alice", idempotency_key=key)


# --------------------------------------------------------------------------- #
# 1a — the work queue: a report that fails after it decided, before it landed
# --------------------------------------------------------------------------- #


def test_a_report_that_fails_before_the_write_lands_leaves_the_lease_intact(queue):
    """`report_result` consumes the lease and may close the run in ONE write. If
    that write never happens the item must still be leased, at the same attempt,
    so the worker's retry is the ordinary one."""
    item = queue.create("work")
    queue.claim(item.id, "alice")
    held = queue.get(item.id)
    before = rows_of(queue)

    # `report_result` always hands `_mutate` the run-close cascade; making it
    # raise is "the process failed after deciding the report, before it landed".
    queue._run_cascade = boom
    with pytest.raises(RuntimeError, match="injected fault"):
        queue.report_result(item.id, held.lease_attempt, WorkStatus.DONE, result="x",
                            submitted_by="alice")

    assert rows_of(queue) == before, "nothing was written: wholly BEFORE"
    still = queue.get(item.id)
    assert (still.status, still.leased_by, still.lease_attempt) == (
        WorkStatus.IN_PROGRESS, "alice", held.lease_attempt)

    del queue._run_cascade  # the fault clears
    done = queue.report_result(item.id, held.lease_attempt, WorkStatus.DONE, result="x",
                               submitted_by="alice")
    assert done.status is WorkStatus.DONE, "the retry converges"


def test_a_cascade_that_dies_midway_rolls_the_whole_close_back(library, queue):
    """The §5.5 close writes the child AND its container. The child's UPDATE (or
    the file's tmp copy) has already happened when the container's write blows
    up; the child must not be left `done` under a container that never closed.

    The poison is a value the container's update cannot be bound/serialised
    with, so the failure is inside the backend's own write path rather than in a
    mock of it: SQLite/Postgres refuse the bind after the child's UPDATE ran,
    and JSONL's `json.dumps` raises while the tmp file is half-written."""
    run = a_run(library, queue)
    first, second = (s["itemId"] for s in run["steps"])
    finish(queue, first)
    queue.claim(second, "alice", capabilities=["verify"])
    attempt = queue.get(second).lease_attempt
    before = rows_of(queue)

    real = queue._run_cascade

    def poisoned(rows, landed):
        closes = real(rows, landed)
        assert closes, "the last step landing must close the run, or this proves nothing"
        for update in closes.values():
            update["result"] = object()
        return closes

    queue._run_cascade = poisoned
    with pytest.raises(Exception):
        queue.report_result(second, attempt, WorkStatus.DONE, result="did it",
                            attestation=ATTESTATION, submitted_by="alice")

    assert rows_of(queue) == before, "child and container both unchanged: wholly BEFORE"
    assert queue.get(second).status is WorkStatus.IN_PROGRESS
    assert queue.get(run["runId"]).status is WorkStatus.PENDING

    del queue._run_cascade
    queue.report_result(second, attempt, WorkStatus.DONE, result="did it",
                        attestation=ATTESTATION, submitted_by="alice")
    assert queue.get(second).status is WorkStatus.DONE
    assert queue.get(run["runId"]).status is WorkStatus.DONE, "the retry closes the run"


def test_a_retry_with_the_same_key_after_a_failed_report_records_exactly_once(queue):
    item = queue.create("work")
    queue.claim(item.id, "alice")
    attempt = queue.get(item.id).lease_attempt

    queue._run_cascade = boom
    with pytest.raises(RuntimeError):
        queue.report_result(item.id, attempt, WorkStatus.DONE, result="x",
                            idempotency_key="k-1", submitted_by="alice")
    del queue._run_cascade

    first = queue.report_result(item.id, attempt, WorkStatus.DONE, result="x",
                                idempotency_key="k-1", submitted_by="alice")
    again = queue.report_result(item.id, attempt, WorkStatus.DONE, result="x",
                                idempotency_key="k-1", submitted_by="alice")
    assert first.status is again.status is WorkStatus.DONE
    assert again.lease_attempt == first.lease_attempt == attempt + 1, "the attempt advanced once"


def test_a_stranded_container_is_repaired_by_the_sweep(library, queue):
    """The one crash window the cascade cannot cover by construction is covered
    by `close_finished_runs`: the child committed, the container never heard.
    Here it is reached by failing the cascade AFTER committing the child alone."""
    run = a_run(library, queue)
    for entry in run["steps"]:
        finish(queue, entry["itemId"])
    assert queue.get(run["runId"]).status is WorkStatus.DONE
    # Reopen the container: the state a death between the two writes would leave.
    queue._mutate(run["runId"], lambda _i: {"status": WorkStatus.PENDING, "result": None})

    assert queue.close_finished_runs() == [run["runId"]]
    assert queue.get(run["runId"]).status is WorkStatus.DONE
    assert queue.close_finished_runs() == [], "and it is idempotent"


# --------------------------------------------------------------------------- #
# 1b — memory: `_record` is `_put`, then `_audit`, then `announce`
# --------------------------------------------------------------------------- #

LEVELS = ("all", "internal", "confidential", "private")
ACME = Clearance({"acme": "internal"}, LEVELS)
PROV = {"kind": "run", "ref": "run:w-0001"}

JSONL_AUDIT_GAP = (
    "REAL DEFECT (known-issues F1): the JSONL MemoryStore._record does `_put` (atomic file "
    "replace) and then `_audit` (separate append) with no shared transaction. A failure "
    "between them leaves an item the audit log has no row for, and nothing repairs it. The "
    "SQL backends write both in one transaction and hold this property."
)


@pytest.fixture()
def store(backend, tmp_path, request):
    if backend == "jsonl":
        return MemoryStore(tmp_path / "memory.jsonl", hub_id="hub-a")
    if backend == "postgres":
        return SqlMemoryStore(request.getfixturevalue("_pg_dsn"), hub_id="hub-a")
    return SqlMemoryStore(tmp_path / "agentco.sqlite3", hub_id="hub-a")


def expect_jsonl_gap(backend, request):
    """F1 is fixed (`_record` writes the audit row first); kept so the two
    tests read as they were written against the defect."""


def lesson(store, **over):
    fields = {"title": "Retry once", "body": "A stalled export resumes after one retry.",
              "provenance": PROV, **over}
    return store.create(actor="kofi", clearance=ACME, owner="acme", level="internal",
                        layer="shared", **fields)


def audit_versions(store, item_id) -> list[int]:
    return [e["version"] for e in store._audits(item_id)]


def assert_every_item_is_audited(store):
    """The invariant `_record` exists to keep: an item's current version has an
    audit row, and the audit chain has no hole."""
    for item in store._all():
        versions = audit_versions(store, item.item_id)
        assert item.version in versions, (
            f"{item.item_id} is at version {item.version} but its audit log holds {versions}")
        assert versions == list(range(1, item.version + 1)), f"audit gap for {item.item_id}: {versions}"


def test_create_failing_between_the_item_and_its_audit_row_is_not_half_written(
        store, backend, request):
    expect_jsonl_gap(backend, request)
    store._audit = boom
    with pytest.raises(RuntimeError, match="injected fault"):
        lesson(store)
    del store._audit

    assert_every_item_is_audited(store)
    retried = lesson(store)
    assert audit_versions(store, retried.item_id) == [1], "the retry itself is whole"


def test_revise_failing_between_the_item_and_its_audit_row_leaves_no_audit_gap(
        store, backend, request):
    """Revise is the sharper case: the item is already v2 on disk, so a retry
    bumps to v3 and the audit log permanently lacks v2 — history that cannot be
    reconstructed, on a store whose audit log is its provenance."""
    expect_jsonl_gap(backend, request)
    item = lesson(store)
    store._audit = boom
    with pytest.raises(RuntimeError, match="injected fault"):
        store.revise(item.item_id, actor="kofi", clearance=ACME, body="Retry twice.")
    del store._audit

    store.revise(item.item_id, actor="kofi", clearance=ACME, body="Retry twice, really.")
    assert_every_item_is_audited(store)


def test_an_item_write_failing_after_its_audit_row_leaves_history_true(store):
    """F1's other side, now that the audit row is written first: the item write
    fails after it. The orphan row describes a version the item never reached,
    so history must not show it, and the retry's own row is the one kept."""
    item = lesson(store)
    real = store._put

    def fail_put(_item):
        raise RuntimeError("injected fault: after the audit row")

    store._put = fail_put
    with pytest.raises(RuntimeError, match="injected fault"):
        store.revise(item.item_id, actor="kofi", clearance=ACME, body="Retry twice.")
    store._put = real

    assert store.get(item.item_id, ACME).version == 1
    assert audit_versions(store, item.item_id) == [1], "an unapplied attempt is not history"
    store.revise(item.item_id, actor="kofi", clearance=ACME, body="Retry twice, really.")
    assert_every_item_is_audited(store)
    assert store.history(item.item_id, ACME)[-1]["record"]["body"] == "Retry twice, really."


def test_a_failing_announcement_never_separates_an_item_from_its_audit_row(store):
    """The feed announcement is the last step. Whichever side of the commit a
    backend puts it on (JSONL: after both writes; SQL: inside the transaction, so
    the whole create rolls back), the item and its audit row stay together."""
    store.announce = boom
    with pytest.raises(RuntimeError, match="injected fault"):
        lesson(store)
    store.announce = None

    assert_every_item_is_audited(store)
    assert audit_versions(store, lesson(store).item_id) == [1]


# --------------------------------------------------------------------------- #
# 1c — outbox: publish, then receipt
# --------------------------------------------------------------------------- #


@pytest.fixture()
def box(tmp_path):
    return Outbox(tmp_path / ".agentco")


def report_line(box, item_id, attempt):
    return box.push("work_report", {"itemId": item_id, "attempt": attempt, "status": "done"})


def hub_publisher(queue, calls):
    """The hub's side of `work_report`: the line id IS the idempotency key
    (`registry_publisher`), landing on whichever backend the test is on."""
    def publish(record):
        calls.append(record["line_id"])
        payload = record["payload"]
        item = queue.report_result(payload["itemId"], payload["attempt"], WorkStatus.DONE,
                                   result="r", idempotency_key=record["line_id"])
        return {"id": item.id, "status": item.status.value}
    return publish


def test_a_drain_that_dies_after_publishing_republishes_and_the_hub_records_once(box, queue):
    item = queue.create("work")
    queue.claim(item.id, "alice")
    attempt = queue.get(item.id).lease_attempt
    line = report_line(box, item.id, attempt)
    calls: list[str] = []

    box.settle = boom  # published, then the drainer dies before the receipt
    with pytest.raises(RuntimeError, match="injected fault"):
        drain(box, hub_publisher(queue, calls))
    del box.settle

    assert [r["line_id"] for r in box.pending()] == [line["line_id"]], "the line is still owed"
    assert box.read_receipts() == []
    landed = queue.get(item.id)
    assert landed.status is WorkStatus.DONE and landed.lease_attempt == attempt + 1

    result = drain(box, hub_publisher(queue, calls))  # also proves the drain lock was released
    assert result["state"] == "drained" and result["published"] == 1 and result["refused"] == 0
    assert calls == [line["line_id"]] * 2, "published twice: at-least-once, as documented"
    after = queue.get(item.id)
    assert (after.status, after.lease_attempt, after.result) == (
        landed.status, landed.lease_attempt, landed.result), "the second publish changed nothing at the hub"
    assert box.pending() == [] and len(box.read_receipts()) == 1


def test_a_settle_that_fails_after_removing_the_lines_does_not_lose_their_receipts(box, queue):
    item = queue.create("work")
    queue.claim(item.id, "alice")
    line = report_line(box, item.id, queue.get(item.id).lease_attempt)

    real_append = box._append_jsonl

    def fail_on_receipts(path, rows, keep=None):
        if Path(path) == box.receipts_path:
            raise RuntimeError("injected fault")
        return real_append(path, rows, keep=keep)

    box._append_jsonl = fail_on_receipts
    with pytest.raises(RuntimeError, match="injected fault"):
        drain(box, hub_publisher(queue, []))
    del box._append_jsonl

    drain(box, hub_publisher(queue, []))  # the next scheduled drain
    learned = [r for r in box.read_receipts() if r["line_id"] == line["line_id"]]
    assert learned or box.pending(), (
        "the line is neither pending nor has a receipt: its outcome is gone")


# --------------------------------------------------------------------------- #
# 1d — the change feed: prune is a floor upsert plus a delete
# --------------------------------------------------------------------------- #

# The feed lives in the registry DB, so it has no JSONL form. (Not imported from conftest:
# `tests/network/conftest.py` shares that module name.)
SQL_BACKENDS = ("sqlite", "postgres") if os.environ.get("AGENTCO_TEST_PG") else ("sqlite",)
OLD, NEW, CUT = "2026-01-01T00:00:00+00:00", "2026-10-18T00:00:00+00:00", "2026-06-01T00:00:00+00:00"


@pytest.fixture(params=SQL_BACKENDS)
def conn(request, tmp_path):
    if request.param == "postgres":
        return db.connect(request.getfixturevalue("_pg_dsn"))
    return db.connect(tmp_path / "registry.sqlite3")


class FailsOn:
    """A connection that raises on the first statement containing `needle`.

    Wraps rather than patches: `sqlite3.Connection` is a C type whose methods
    cannot be replaced on an instance. `with conn:` is delegated so the
    transaction the code under test opens is the real one.
    """

    def __init__(self, inner, needle):
        self._inner, self._needle, self.armed = inner, needle, True

    def execute(self, sql, *args, **kwargs):
        if self.armed and self._needle in sql:
            self.armed = False
            raise RuntimeError("injected fault")
        return self._inner.execute(sql, *args, **kwargs)

    def __enter__(self):
        self._inner.__enter__()
        return self

    def __exit__(self, *exc):
        return self._inner.__exit__(*exc)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def feed(conn, n, at):
    return [events.append(conn, kind="ScopeClaimed", actor="a", payload={"n": i}, occurred_at=at)["seq"]
            for i in range(n)]


@pytest.mark.parametrize("failing", ["INSERT INTO meta", "DELETE FROM events"])
def test_a_prune_that_fails_midway_neither_moves_the_floor_nor_deletes(conn, failing):
    """The floor is written before the delete, in one transaction. If the delete
    fails the floor must not have moved: a floor ahead of the data would refuse
    cursors that are still perfectly resumable, and a delete without its floor
    would be the silent gap the floor exists to prevent."""
    old, new = feed(conn, 4, OLD), feed(conn, 3, NEW)
    before = [e["seq"] for e in events.read(conn, limit=500)["events"]]

    with pytest.raises(RuntimeError, match="injected fault"):
        events.prune(FailsOn(conn, failing), before=CUT)

    assert events.retention_floor(conn) == 1, "the floor did not move"
    assert [e["seq"] for e in events.read(conn, limit=500)["events"]] == before
    # An old cursor still resumes: nothing was lost, so nothing is refused.
    assert events.read(conn, since=events.encode_cursor(old[1]), limit=500)["events"]

    done = events.prune(conn, before=CUT)  # the retry converges
    assert done["retainedFrom"] == new[0] and done["deleted"] == 4
    assert events.retention_floor(conn) == new[0]
    assert [e["seq"] for e in events.read(conn, limit=500)["events"]] == new


def test_an_event_append_that_fails_leaves_no_row_and_the_next_one_lands(conn):
    guard = FailsOn(conn, "INSERT INTO events")
    with pytest.raises(RuntimeError, match="injected fault"):
        events.append(guard, kind="ScopeClaimed", actor="a", payload={"n": 0})
    assert events.read(conn, limit=10)["events"] == []
    stored = events.append(conn, kind="ScopeClaimed", actor="a", payload={"n": 1})
    assert [e["seq"] for e in events.read(conn, limit=10)["events"]] == [stored["seq"]]


# --------------------------------------------------------------------------- #
# 2 — real process death
# --------------------------------------------------------------------------- #


def run_script(source: str, *argv: str, kill_on: Path | None = None) -> subprocess.Popen:
    """Run `source` in a fresh interpreter and wait until it is dead.

    With `kill_on`, the child parks itself inside the write and the PARENT
    delivers SIGKILL once that file appears. Without it, the child kills itself
    at the moment of the torn write — deterministic, and as unclean as any other
    SIGKILL. Always reaped: the `finally` is what keeps a failed test from
    leaving an interpreter behind.
    """
    proc = subprocess.Popen([sys.executable, "-c", textwrap.dedent(source), *argv],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        if kill_on is not None:
            deadline = time.monotonic() + PROCESS_TIMEOUT_S
            while not kill_on.exists():
                assert proc.poll() is None, f"child exited early: {proc.stderr.read().decode()}"
                assert time.monotonic() < deadline, "child never reached the write"
                time.sleep(0.02)
            proc.send_signal(signal.SIGKILL)
        proc.wait(timeout=PROCESS_TIMEOUT_S)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        for stream in (proc.stdout, proc.stderr):
            if stream:
                stream.close()
    assert proc.returncode == -signal.SIGKILL, "the point is a process that did not clean up"
    return proc


# A wrapper that writes half of what it is given, makes it durable, and dies.
TORN = """
import os, signal
class Torn:
    def __init__(self, f): self.f = f
    def __enter__(self): return self
    def __exit__(self, *a): self.f.close()
    def write(self, data):
        self.f.write(data[: max(1, len(data) // 2)]); self.f.flush(); os.fsync(self.f.fileno())
        os.kill(os.getpid(), signal.SIGKILL)
    def __getattr__(self, name): return getattr(self.f, name)
"""


def test_jsonl_queue_killed_mid_rewrite_keeps_every_committed_row(tmp_path):
    """The queue replaces the whole file via a tmp copy. Killed halfway through
    writing that copy, the real file must be untouched — never a torn line in it —
    and the next write must succeed."""
    path = tmp_path / "work.jsonl"
    queue = Queue(path)
    ids = [queue.create(f"committed {i}").id for i in range(4)]
    before_bytes, before_rows = path.read_bytes(), rows_of(queue)

    run_script(f"""
        import os, sys
        from pathlib import Path
        from agentco.work import Queue
        calls = {{"n": 0}}
        real = os.fdopen
        {textwrap.indent(textwrap.dedent(TORN), "        ").lstrip()}
        class Second(Torn):
            def write(self, data):
                calls["n"] += 1
                if calls["n"] == 2:
                    super().write(data)
                else:
                    self.f.write(data)
        os.fdopen = lambda fd, mode="r", *a, **k: Second(real(fd, mode, *a, **k)) if "b" in mode else real(fd, mode, *a, **k)
        Queue(Path(sys.argv[1])).create("the write that dies")
    """, str(path))

    assert path.read_bytes() == before_bytes, "the committed file is byte-identical"
    reopened = Queue(path)
    assert rows_of(reopened) == before_rows and reopened.quarantined == []
    assert sorted(i.id for i in reopened.list()) == sorted(ids), "no committed item lost"
    assert reopened.create("after the crash").id, "the next write succeeds"
    assert len(reopened.list()) == 5 and reopened.quarantined == []


def test_sqlite_queue_killed_inside_a_transaction_rolls_back(tmp_path):
    """Killed after the child's UPDATE ran but before COMMIT: reopening must
    show the lease exactly as it was, the file must be sound, and the lock the
    dead process held must not outlive it."""
    path = tmp_path / "agentco.sqlite3"
    queue = SqlQueue(path)
    item = queue.create("work")
    queue.claim(item.id, "alice")
    attempt = queue.get(item.id).lease_attempt
    others = [queue.create(f"other {i}").id for i in range(3)]
    before = rows_of(queue)
    queue.close()

    parked = tmp_path / "parked"
    run_script("""
        import sys, time
        from pathlib import Path
        from agentco.sqlstore import SqlQueue
        from agentco.work import WorkStatus
        q = SqlQueue(sys.argv[1])
        def park(rows, landed):
            Path(sys.argv[3]).write_text("in the transaction")  # the UPDATE has run; COMMIT has not
            time.sleep(120)
        q._run_cascade = park
        q.report_result(sys.argv[2], int(sys.argv[4]), WorkStatus.DONE, result="x", submitted_by="alice")
    """, str(path), item.id, str(parked), str(attempt), kill_on=parked)

    reopened = SqlQueue(path)
    assert rows_of(reopened) == before, "the half-done report left no trace: wholly BEFORE"
    assert reopened._conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    held = reopened.get(item.id)
    assert (held.status, held.leased_by, held.lease_attempt) == (WorkStatus.IN_PROGRESS, "alice", attempt)
    assert sorted(i.id for i in reopened.list()) == sorted([item.id, *others])

    done = reopened.report_result(item.id, attempt, WorkStatus.DONE, result="x", submitted_by="alice")
    assert done.status is WorkStatus.DONE, "the retry lands: no lock was left behind"
    assert reopened.create("after the crash").id
    reopened.close()


def test_outbox_push_after_a_killed_push_is_not_lost(tmp_path):
    """`tests/test_outbox_concurrency.py` proves a torn tail costs exactly one
    line when DRAINED. This is the other half: a line pushed AFTER the crash must
    survive too, which it can only do if the append starts on a fresh line."""
    box = Outbox(tmp_path / ".agentco")
    claim = {"repo": "acme/web", "prefixes": ["src/"], "intent": "edit"}
    first = box.push("claim_scope", claim)["line_id"]

    run_script(f"""
        import sys
        import agentco.appendlog as al
        import agentco.outbox as ob
        {textwrap.indent(textwrap.dedent(TORN), "        ").lstrip()}
        real = open
        al.open = lambda p, mode="r", *a, **k: Torn(real(p, mode, *a, **k)) if mode == "a+b" else real(p, mode, *a, **k)
        ob.Outbox(sys.argv[1]).push("claim_scope", {claim!r})
    """, str(box.dir))
    assert not box.path.read_bytes().endswith(b"\n"), "the file really is torn"

    after = box.push("claim_scope", claim)["line_id"]
    published: list[str] = []
    result = drain(box, lambda r: published.append(r["line_id"]) or {"leaseUid": "l"})

    assert first in published
    assert after in published, "the line pushed after the crash was glued onto the torn tail"
    assert result["quarantined"] == 1, "only the torn fragment is quarantined"


def test_memory_audit_appended_after_a_killed_audit_write_is_not_lost(tmp_path):
    """The JSONL memory audit log is append-only. Killed mid-line, the next
    audit row must start on a fresh line, or the next item's history is silently
    unreadable: `_audits` skips any line that does not parse."""
    path = tmp_path / "memory.jsonl"
    seed = MemoryStore(path, hub_id="hub-a")
    kept = lesson(seed)

    run_script("""
        import sys
        import agentco.appendlog as al
        import agentco.memory as mem
        from agentco.clearance import Clearance
        """ + textwrap.indent(textwrap.dedent(TORN), "        ").lstrip() + """
        real = open
        al.open = lambda p, mode="r", *a, **k: Torn(real(p, mode, *a, **k)) if str(p).endswith(".audit") else real(p, mode, *a, **k)
        store = mem.MemoryStore(sys.argv[1], hub_id="hub-a")
        store.create(actor="kofi", clearance=Clearance({"acme": "internal"}, ("all", "internal", "confidential", "private")),
                     owner="acme", level="internal", layer="shared", title="dies", body="b",
                     provenance={"kind": "run", "ref": "run:w-0001"})
    """, str(path))

    store = MemoryStore(path, hub_id="hub-a")
    assert store.get(kept.item_id, ACME).title == kept.title, "the committed item survived"
    survivor = lesson(store, title="written after the crash")
    assert [e["action"] for e in store.history(survivor.item_id, ACME)] == ["create"], (
        "the audit row written after the crash was glued onto the torn fragment and is unreadable")
    assert [e["action"] for e in store.history(kept.item_id, ACME)] == ["create"]


def test_appendlog_starts_a_fresh_line_after_a_torn_tail(tmp_path):
    """The shared helper behind F3/F4, directly: a torn tail stays its own line,
    the new record lands whole, an intact file gets no blank line, and an empty
    append writes nothing."""
    from agentco import appendlog

    path = tmp_path / "log.jsonl"
    appendlog.append_lines(path, [b'{"a":1}'])
    appendlog.append_lines(path, [])
    assert path.read_bytes() == b'{"a":1}\n'
    path.write_bytes(path.read_bytes() + b'{"torn":')
    appendlog.append_lines(path, [b'{"b":2}', b'{"c":3}'])
    assert path.read_bytes().split(b"\n") == [b'{"a":1}', b'{"torn":', b'{"b":2}', b'{"c":3}', b""]
