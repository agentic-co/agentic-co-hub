"""R10-R15 from the 2026-11-07 review (docs/known-issues.md), auth lane.

Each test states the property that should hold and began life as a strict
xfail; the fix commit removes the marker (see the conventions docstring of
`tests/test_adversarial_findings.py`).
"""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import timedelta

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519
from fastapi.testclient import TestClient

from agentco import auth, clearance, db, envelope, humankeys, hubid
from agentco.app import MAX_BODY_BYTES, create_app
from agentco.errors import Refusal
from agentco.work import Queue, WorkStatus
from tests.test_envelope import HUMAN, NOW, payload_for, row, send
from tests.test_humankeys import fingerprint, pem

SECRETS = {"alice": "alice-secret", "bob": "bob-secret", "op": "op-secret",
           "alice-bot": "alice-bot-secret"}


def _keys(tmp_path, monkeypatch):
    path = tmp_path / "keys.json"
    table = {"alice": SECRETS["alice"], "bob": SECRETS["bob"], "op": SECRETS["op"],
             "alice-bot": {"secret": SECRETS["alice-bot"], "owner": "alice"}}
    path.write_text(json.dumps(table))
    path.chmod(0o600)
    monkeypatch.setenv(auth.KEYS_ENV_VAR, str(path))
    return path


def _client(tmp_path, monkeypatch):
    _keys(tmp_path, monkeypatch)
    return TestClient(create_app(
        db_path=str(tmp_path / "api.sqlite3"), operator="op",
        work_store=str(tmp_path / "work.jsonl"), sop_store=str(tmp_path / "sops.jsonl")))


def call(client, actor, method, path, body=None, query="", raw=None):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else b"")
    ts = str(int(time.time()))
    headers = {"X-AgentCo-Actor": actor, "X-AgentCo-Timestamp": ts, "Content-Type": "application/json",
               "X-AgentCo-Signature": auth.sign(SECRETS[actor], method, path, ts, data, query=query)}
    return client.request(method, path + (f"?{query}" if query else ""), content=data, headers=headers)


# --- R10 ---------------------------------------------------------------------


def _filed(client):
    ids = {}
    for key, who in (("alice", "alice"), ("bob", "bob"), ("bot", "alice-bot")):
        r = call(client, "op", "POST", "/work", {"title": f"for {who}", "assignedAgent": who})
        ids[key] = r.json()["item"]["id"]
    return ids


def test_a_plain_work_list_shows_a_non_operator_only_its_partys_items(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ids = _filed(client)
    seen = {i["id"] for i in call(client, "alice", "GET", "/work").json()["items"]}
    assert seen == {ids["alice"], ids["bot"]}
    assert {i["id"] for i in call(client, "bob", "GET", "/work").json()["items"]} == {ids["bob"]}
    assert {i["id"] for i in call(client, "op", "GET", "/work").json()["items"]} == set(ids.values())


def test_a_single_item_read_of_another_partys_item_answers_like_a_missing_id(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ids = _filed(client)
    assert call(client, "alice", "GET", f"/work/{ids['alice']}").status_code == 200
    assert call(client, "alice", "GET", f"/work/{ids['bot']}").status_code == 200
    other = call(client, "alice", "GET", f"/work/{ids['bob']}")
    missing = call(client, "alice", "GET", "/work/does-not-exist")
    assert other.status_code == 404 and other.json()["code"] == missing.json()["code"]
    assert call(client, "op", "GET", f"/work/{ids['bob']}").status_code == 200


# --- R11 ---------------------------------------------------------------------


def test_mcp_local_mode_resolves_owner_parties_like_the_http_app(tmp_path, monkeypatch):
    pytest.importorskip("mcp")
    from agentco import mcp_server

    _keys(tmp_path, monkeypatch)
    opened = []
    real = mcp_server.open_queue

    def spy(*a, **kw):
        q = real(*a, **kw)
        opened.append(q)
        return q

    monkeypatch.setattr(mcp_server, "open_queue", spy)
    mcp_server.create_server(db_path=str(tmp_path / "r.sqlite3"), work_store=str(tmp_path / "w.jsonl"),
                             sop_store=str(tmp_path / "s.jsonl"), actor="alice-bot")
    assert opened and opened[0].party_of("alice-bot") == "alice"


# --- R12 ---------------------------------------------------------------------


def test_party_of_fails_closed_when_the_owner_table_cannot_be_read(tmp_path):
    queue = Queue(tmp_path / "w.jsonl")

    def broken():
        raise OSError("key table unreadable")

    queue.owners = broken
    with pytest.raises(Refusal) as refused:
        queue.party_of("alice-bot")
    assert "key table" in refused.value.message.lower()


# --- R13 ---------------------------------------------------------------------


def test_the_body_is_never_read_when_content_length_is_over_the_limit(tmp_path, monkeypatch):
    from starlette.requests import Request

    client = _client(tmp_path, monkeypatch)
    reads = []
    real = Request.body

    async def spy(self):
        reads.append(1)
        return await real(self)

    monkeypatch.setattr(Request, "body", spy)
    r = call(client, "alice", "POST", "/work", raw=b"x" * (MAX_BODY_BYTES + 1))
    assert r.status_code == 413 and r.json()["code"] == "body_too_large"
    assert not reads, "the oversized body was buffered before being refused"


def test_an_unauthenticated_request_leaves_no_calls_row_and_parses_no_body(tmp_path, monkeypatch):
    from agentco import app as app_module

    client = _client(tmp_path, monkeypatch)
    parsed = []
    real = app_module._observed_label
    monkeypatch.setattr(app_module, "_observed_label",
                        lambda request, body: parsed.append(1) or real(request, body))
    r = client.post("/work", content=b'{"title": "t"}', headers={"X-AgentCo-Actor": "alice"})
    assert r.status_code == 401
    assert not parsed, "the body was inspected before the caller authenticated"
    conn = sqlite3.connect(str(tmp_path / "api.sqlite3"))
    try:
        n = conn.execute("SELECT COUNT(*) FROM calls WHERE actor = '-'").fetchone()[0]
    finally:
        conn.close()
    assert n == 0


# --- R14 ---------------------------------------------------------------------


def test_a_future_dated_envelope_keeps_its_nonce_until_it_can_no_longer_be_valid(tmp_path):
    conn = db.connect(tmp_path / "r.sqlite3")
    hubid.ensure(conn)
    queue = Queue(tmp_path / "work.jsonl", humans=["dana", "eli"])
    queue.envelope_required_for = envelope.required_for(conn)
    private = ed25519.Ed25519PrivateKey.generate()
    humankeys.enrol(conn, human="dana", public_key=pem(private), fingerprint=fingerprint(private),
                    by="op", humans={"dana", "eli"}, label="acme-hub")
    item = queue.create("ship", verify=HUMAN)
    claimed = queue.claim(item.id, "hub-worker")
    queue.report_result(item.id, claimed.lease_attempt, WorkStatus.DONE)
    payload = payload_for(conn, item.id, private, issued_at=int(NOW.timestamp()) + 290)
    send(conn, queue, payload, private, fresh=300, grace=60)
    # past arrival + fresh + grace, but still inside issued_at + fresh
    envelope.prune(conn, now=NOW + timedelta(seconds=300 + 60 + 10), fresh=300, grace=60)
    assert row(conn, payload) is not None


# --- R15 ---------------------------------------------------------------------


def test_clearance_key_table_is_not_reparsed_when_unchanged(tmp_path, monkeypatch):
    path = tmp_path / "keys.json"
    path.write_text(json.dumps({"alice": {"secret": "s", "clearance": "internal"}}))
    monkeypatch.setenv(auth.KEYS_ENV_VAR, str(path))
    parses = []
    real = json.loads
    monkeypatch.setattr(json, "loads", lambda *a, **kw: parses.append(1) or real(*a, **kw))
    levels = ("public", "internal", "restricted")
    first = clearance.actors_from_key_table(levels)
    for _ in range(5):
        again = clearance.actors_from_key_table(levels)
    assert again == first
    assert len(parses) <= 1, f"parsed {len(parses)} times"
    path.write_text(json.dumps({"alice": {"secret": "s", "clearance": "restricted"}, "bob": {"secret": "x", "clearance": "internal"}}))
    assert clearance.actors_from_key_table(levels) != first, "an edit must still be seen at once"


# --- R10 residue: the filer stays in reach -------------------------------------


def test_a_filer_keeps_sight_of_what_they_filed_after_it_is_assigned_elsewhere(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    made = call(client, "alice", "POST", "/work", {"title": "for bob", "assignedAgent": "bob"})
    item_id = made.json()["item"]["id"]
    assert item_id in {i["id"] for i in call(client, "alice", "GET", "/work").json()["items"]}
    assert call(client, "alice", "GET", f"/work/{item_id}").status_code == 200
    # alice's own agent is the same party; a stranger's filing stays hidden
    assert call(client, "alice-bot", "GET", f"/work/{item_id}").status_code == 200
    third = call(client, "op", "POST", "/work", {"title": "x", "assignedAgent": "bob"}).json()["item"]["id"]
    assert call(client, "alice", "GET", f"/work/{third}").status_code == 404
    # and the creator is not caller-settable
    forged = call(client, "bob", "POST", "/work", {"title": "f", "assignedAgent": "bob",
                                                    "metadata": {"filed_by": "alice"}})
    assert forged.status_code >= 400 and forged.json()["code"] == "metadata_reserved"


def test_every_item_a_run_files_is_in_reach_of_the_one_who_filed_it(library, queue):
    from agentco import authz

    gate = {"kind": "deterministic", "check": "make check", "max_park_seconds": 900, "on_timeout": "fail"}
    asop = library.create("two steps", inputs=[], roles={"w": {"kind": "agent"}}, steps=[
        {"name": "one", "role": "w", "purpose": "one", "gate": gate},
        {"name": "two", "role": "w", "purpose": "two", "gate": gate}])
    library.activate(asop.asop_id, 1, author_kind="human")
    library.run(asop.asop_id, queue, bindings={"w": "kofi"}, filed_by="dana")
    items = queue.list()
    assert len(items) >= 3
    assert all(authz.item_in_reach("dana", i, {}) for i in items)
    assert not any(authz.item_in_reach("ana", i, {}) for i in items if i.assigned_agent == "kofi")


def test_mcp_work_create_records_who_filed_it(tmp_path):
    pytest.importorskip("mcp")
    from agentco import authz
    from agentco.mcp_server import create_server
    from agentco.work import WorkItem

    mcp = create_server(db_path=str(tmp_path / "r.sqlite3"), work_store=str(tmp_path / "w.jsonl"),
                        sop_store=str(tmp_path / "s.jsonl"), actor="dana")
    made = mcp._tool_manager.get_tool("work_create").fn("t", assigned_agent="kofi")
    item = WorkItem.from_json(json.dumps(made["item"] if "item" in made else made))
    assert authz.item_in_reach("dana", item, {})
