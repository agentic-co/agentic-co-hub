"""Regressions for the 2026-11-07 code review, findings R1-R3.

Each test was run against the pre-fix code and failed there, which is what
makes it a regression test rather than a description of current behaviour
(see the conventions in `tests/test_adversarial_findings.py`).

- R1: a non-string `blockedBy` element was stored, and every later `ready()`
  raised `TypeError` — one filing took down every worker's pull.
- R2: an `X-AgentCo-Timestamp` of "nan" parsed, and every comparison with NaN
  is False, so the replay window never closed on it.
- R3: `SqlMemoryStore._in_tx` was shared across threads, so a reader on
  another thread skipped the read lock and saw uncommitted rows.
"""

from __future__ import annotations

import json
import threading
import time

import pytest

from agentco import auth
from agentco.errors import Refusal, Unauthenticated
from agentco.memory import SqlMemoryStore
from agentco.work import Queue, WorkItem


# --------------------------------------------------------------------------- #
# R1 — one malformed filing must not poison the queue for everyone


@pytest.mark.parametrize(
    "field, value",
    [
        ("blocked_by", [{"a": 1}]),
        ("blocked_by", [1]),
        ("blocked_by", [""]),
        ("blocked_by", "w-1234abcd"),  # a bare string, which list() would split
        ("requires", [["nested"]]),
        ("requires", 5),
    ],
)
def test_create_refuses_a_malformed_id_list(tmp_path, field, value):
    queue = Queue(str(tmp_path / "w.jsonl"))
    with pytest.raises(Refusal) as caught:
        queue.create("x", **{field: value})
    assert caught.value.code == "invalid_request"
    assert queue.list() == []  # refused before the store was touched


def test_ready_survives_a_malformed_row_already_on_disk(tmp_path):
    path = tmp_path / "w.jsonl"
    queue = Queue(str(path))
    good = queue.create("good")
    queue.create("poison")
    # Simulate a row written before `build_item` refused this shape.
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    for row in rows:
        if row.get("title") == "poison":
            row["blocked_by"] = [{"a": 1}]
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))

    ready = Queue(str(path)).ready(agent="w")
    assert [item.id for item in ready] == [good.id]  # poison stays blocked, nobody crashes


def test_a_non_string_blocker_is_never_met():
    item = WorkItem(id="w-1", title="x", blocked_by=[{"a": 1}])  # type: ignore[list-item]
    assert item.unmet_blockers({"w-2"}) == [{"a": 1}]


def test_http_create_refuses_malformed_blocked_by_with_400(tmp_path):
    from fastapi.testclient import TestClient

    from agentco.app import create_app

    keys = {"dana": "dana-secret", "operator": "op-secret"}
    client = TestClient(create_app(
        db_path=str(tmp_path / "api.sqlite3"),
        keys=keys,
        operator="operator",
        work_store=str(tmp_path / "work.jsonl"),
        sop_store=str(tmp_path / "sops.jsonl"),
    ))

    def post(path, body):
        raw = json.dumps(body).encode()
        ts = str(int(time.time()))
        return client.post(path, content=raw, headers={
            "X-AgentCo-Actor": "dana",
            "X-AgentCo-Timestamp": ts,
            "X-AgentCo-Signature": auth.sign(keys["dana"], "POST", path, ts, raw),
            "Content-Type": "application/json",
        })

    refused = post("/work", {"title": "x", "blockedBy": [{"a": 1}]})
    assert refused.status_code == 400, refused.text
    assert refused.json()["code"] == "invalid_request"

    post("/work", {"title": "fine"})
    pulled = post("/work/pull", {"capabilities": []})
    assert pulled.status_code == 200, pulled.text


# --------------------------------------------------------------------------- #
# R2 — a non-finite timestamp must not switch off the replay window


@pytest.mark.parametrize("timestamp", ["nan", "NaN", "-nan", "inf", "-inf", "Infinity"])
def test_non_finite_timestamp_is_unauthenticated(timestamp):
    keys = {"a": "s"}
    headers = {
        "x-agentco-actor": "a",
        "x-agentco-timestamp": timestamp,
        "x-agentco-signature": auth.sign("s", "GET", "/events", timestamp, b""),
    }
    for now in (0.0, 1e9, 4e9):
        with pytest.raises(Unauthenticated):
            auth.authenticate(headers, "GET", "/events", b"", keys=keys, now=now)


def test_a_finite_timestamp_still_authenticates():
    keys = {"a": "s"}
    ts = "1000000000"
    headers = {
        "x-agentco-actor": "a",
        "x-agentco-timestamp": ts,
        "x-agentco-signature": auth.sign("s", "GET", "/events", ts, b""),
    }
    assert auth.authenticate(headers, "GET", "/events", b"", keys=keys, now=1e9) == "a"


# --------------------------------------------------------------------------- #
# R3 — another thread's transaction is not this thread's transaction


def test_reader_on_another_thread_never_sees_an_uncommitted_row(tmp_path):
    store = SqlMemoryStore(tmp_path / "mem.sqlite3")
    inside, seen = threading.Event(), {}

    def writer():
        try:
            with store._locked():
                store._conn().execute(
                    "INSERT INTO memory_state (item_id, state) VALUES ('ghost', '{}')"
                )
                inside.set()
                time.sleep(0.3)
                raise RuntimeError("roll back")
        except RuntimeError:
            pass

    def reader():
        inside.wait(5)
        seen["rows"] = [r["item_id"] for r in store._query("SELECT item_id FROM memory_state")]

    threads = [threading.Thread(target=writer), threading.Thread(target=reader)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)

    assert seen["rows"] == []
    assert store._query("SELECT item_id FROM memory_state") == []


def test_the_writing_thread_still_reads_its_own_transaction(tmp_path):
    store = SqlMemoryStore(tmp_path / "mem.sqlite3")
    with store._locked():
        store._conn().execute("INSERT INTO memory_state (item_id, state) VALUES ('mine', '{}')")
        assert [r["item_id"] for r in store._query("SELECT item_id FROM memory_state")] == ["mine"]
        assert store._in_tx
    assert not store._in_tx
