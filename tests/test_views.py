"""`GET /views/me` (ADR 0010 §6 step 1): the signer's lens.

What would fail if the feature were dishonest: a viewer seeing another actor's
items; a query parameter widening the view instead of being refused; a done
item still listed as something to do; a vehicle (a gate waiting on the viewer)
landing in the wrong panel; the oldest open item not coming first.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from agentco import auth, views
from agentco.app import create_app
from agentco.work import Queue, WorkItem, WorkStatus

SECRETS = {"dana": "dana-secret", "eli": "eli-secret", "operator": "op-secret"}


@pytest.fixture()
def paths(tmp_path, monkeypatch):
    keys = tmp_path / "keys.json"
    keys.write_text(json.dumps(SECRETS))
    keys.chmod(0o600)
    monkeypatch.setenv(auth.KEYS_ENV_VAR, str(keys))
    return tmp_path


@pytest.fixture()
def client(paths):
    return TestClient(create_app(
        db_path=str(paths / "api.sqlite3"), operator="operator",
        work_store=str(paths / "work.jsonl"), sop_store=str(paths / "sops.jsonl")))


def call(client, actor, path, query=""):
    ts = str(int(time.time()))
    headers = {"X-AgentCo-Actor": actor, "X-AgentCo-Timestamp": ts,
               "X-AgentCo-Signature": auth.sign(SECRETS[actor], "GET", path, ts, b"", query=query)}
    return client.get(path + (f"?{query}" if query else ""), headers=headers)


def test_the_view_is_the_signers_and_splits_gates_from_work(client, paths):
    q = Queue(paths / "work.jsonl")
    target = q.create("Ship the fix", assigned_agent="eli")
    q.create("Write the notes", assigned_agent="dana")
    q.create("Verify: Ship the fix", assigned_agent="dana", natural_key=f"verify:{target.id}:0",
             metadata={"verifies": target.id}, by_plane=True)
    q.create("Someone else's", assigned_agent="eli")
    q.create("Nobody's")

    body = call(client, "dana", "/views/me").json()
    assert body["state"] == "accepted"
    assert body["viewer"] == "dana"
    assert body["hubId"]
    assert [r["title"] for r in body["mine"]] == ["Write the notes"]
    assert [r["title"] for r in body["askedOfMe"]] == ["Verify: Ship the fix"]
    assert body["askedOfMe"][0]["ref"] == f"verify:{target.id}:0"


def test_a_parameter_is_refused_not_ignored(client):
    response = call(client, "dana", "/views/me", query="agent=eli")
    assert response.status_code == 400
    assert response.json()["code"] == "view_takes_no_parameter"


def test_the_operator_gets_a_lens_too_not_the_whole_queue(client, paths):
    Queue(paths / "work.jsonl").create("Dana's", assigned_agent="dana")
    body = call(client, "operator", "/views/me").json()
    assert body["mine"] == [] and body["askedOfMe"] == []


def test_terminal_items_leave_the_view(paths):
    q = Queue(paths / "work.jsonl")
    item = q.create("Done soon", assigned_agent="dana")
    leased = q.claim(item.id, "dana")
    q.report_result(item.id, leased.lease_attempt, WorkStatus.DONE, result="ok")
    assert views.lens(q, viewer="dana")["mine"] == []


def test_oldest_open_item_comes_first():
    class Fixed:
        def __init__(self, items):
            self.items = items

        def list(self):
            return self.items

    now = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)
    stamp = lambda hours: (now - timedelta(hours=hours)).isoformat()
    items = [WorkItem(id="a", title="Two hours", assigned_agent="dana", created_at=stamp(2)),
             WorkItem(id="b", title="Two days", assigned_agent="dana", created_at=stamp(48)),
             WorkItem(id="c", title="Just now", assigned_agent="dana", created_at=stamp(0))]
    rows = views.lens(Fixed(items), viewer="dana", now=now)["mine"]
    assert [r["title"] for r in rows] == ["Two days", "Two hours", "Just now"]
    assert rows[0]["ageSeconds"] == 48 * 3600
