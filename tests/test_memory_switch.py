"""Shared memory is OFF unless AGENTCO_MEMORY=on (owner decision 2026-10-18, option B).

While off: every /memory route answers the same documented refusal (404
`memory_disabled`), the memory store is never opened, the migration still
applies and leaves empty tables, and the pulse and MCP `whoami` say
"disabled". While on: exactly the behaviour the memory tests assert.
"""

from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from agentco import auth
from agentco.app import ROUTE_BODIES, create_app
from agentco.memory import memory_enabled

KEYS = {"dana": "dana-secret"}
MEMORY_ROUTES = sorted(k for k in ROUTE_BODIES if k[1].startswith("/memory"))


def make(tmp_path, **kw):
    return TestClient(create_app(
        db_path=str(tmp_path / "api.sqlite3"), keys=KEYS, operator="dana", humans=["dana"],
        work_store=str(tmp_path / "work.jsonl"), sop_store=str(tmp_path / "sops.jsonl"),
        memory_store=str(tmp_path / "memory.jsonl"), hub_clearance={"acme": "private"},
        clearances={"dana": {"acme": "private"}}, **kw))


def call(client, method, path, body=None):
    raw = json.dumps(body).encode() if body is not None else b""
    ts = str(int(time.time()))
    headers = {"X-AgentCo-Actor": "dana", "X-AgentCo-Timestamp": ts,
               "X-AgentCo-Signature": auth.sign(KEYS["dana"], method, path, ts, raw),
               "Content-Type": "application/json"}
    return client.request(method, path, content=raw or None, headers=headers)


def concrete(path):
    return path.replace("{item_id}", "mem-000000000000")


@pytest.fixture(autouse=True)
def _no_switch_in_env(monkeypatch):
    monkeypatch.delenv("AGENTCO_MEMORY", raising=False)


def test_off_is_the_default():
    assert memory_enabled() is False


@pytest.mark.parametrize("value,expected", [("on", True), ("ON", True), ("off", False), ("", False)])
def test_the_switch_reads_on_and_off(value, expected):
    assert memory_enabled(value) is expected


def test_a_misspelt_switch_refuses_to_start(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTCO_MEMORY", "onn")
    with pytest.raises(ValueError, match="expected 'on' or 'off'"):
        make(tmp_path)


def test_every_memory_route_answers_the_same_refusal_when_off(tmp_path):
    client = make(tmp_path)
    assert MEMORY_ROUTES, "the memory routes are declared"
    bodies = set()
    for method, path in MEMORY_ROUTES:
        response = call(client, method, concrete(path), {} if method == "POST" else None)
        assert response.status_code == 404, (method, path, response.text)
        bodies.add(response.text)
    assert len(bodies) == 1, "one uniform answer, whatever the route"
    assert json.loads(bodies.pop())["code"] == "memory_disabled"


def test_off_opens_no_store_and_leaves_the_migrated_tables_empty(tmp_path):
    from agentco import db

    client = make(tmp_path)
    call(client, "POST", "/memory", {"label": {"owner": "acme", "level": "all"}})
    assert client.app.state.memory_enabled is False
    assert not (tmp_path / "memory.jsonl").exists()
    conn = db.connect(tmp_path / "api.sqlite3")
    assert conn.execute("SELECT COUNT(*) AS n FROM memory_items").fetchone()["n"] == 0


def test_on_serves_memory_exactly_as_built(tmp_path):
    client = make(tmp_path, memory_enabled=True)
    created = call(client, "POST", "/memory", {
        "label": {"owner": "acme", "level": "internal"}, "layer": "shared", "title": "t", "body": "b",
        "provenance": {"kind": "run", "ref": "run:w-1"}})
    assert created.status_code == 200, created.text
    assert call(client, "GET", "/memory").json()["count"] == 1


def test_the_environment_turns_it_on(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTCO_MEMORY", "on")
    assert call(make(tmp_path), "GET", "/memory").status_code == 200


def test_the_pulse_and_whoami_report_the_switch(tmp_path, monkeypatch):
    from datetime import datetime, timezone

    from agentco import db, pulse

    conn = db.connect(tmp_path / "r.sqlite3")
    assert pulse.check_plane(conn, now=datetime.now(timezone.utc))[0]["memory"] == "disabled"
    monkeypatch.setenv("AGENTCO_MEMORY", "on")
    assert pulse.check_plane(conn, now=datetime.now(timezone.utc))[0]["memory"] == "enabled"

    from agentco.mcp_server import create_server
    from tests.test_mcp_remote import KEYS as RKEYS, LoopbackRegistry

    remote_client = TestClient(create_app(db_path=str(tmp_path / "m.sqlite3"), keys=RKEYS, operator="operator",
                                          work_store=str(tmp_path / "w2.jsonl"), sop_store=str(tmp_path / "s2.jsonl")))
    server = create_server(registry=LoopbackRegistry("macbook", remote_client))
    assert server._tool_manager.get_tool("whoami").fn()["memory"] == "enabled"
    monkeypatch.setenv("AGENTCO_MEMORY", "off")
    assert server._tool_manager.get_tool("whoami").fn()["memory"] == "disabled"


@pytest.mark.parametrize("enabled", [False, True])
def test_live_wire_both_ways(tmp_path, enabled):
    import socket
    import threading

    import uvicorn

    from agentco.publish import Registry, RegistryError

    app = make(tmp_path, memory_enabled=enabled).app
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    try:
        reg = Registry("dana", KEYS["dana"], f"http://127.0.0.1:{port}")
        if enabled:
            assert reg._call("GET", "/memory")["count"] == 0
        else:
            with pytest.raises(RegistryError) as exc:
                reg._call("GET", "/memory")
            assert exc.value.status == 404 and exc.value.payload["code"] == "memory_disabled"
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_off_the_feed_never_carries_a_memory_event(tmp_path, monkeypatch):
    """M2's walled feed kind: with the switch off there is no store to announce
    one, so neither HTTP nor the local MCP backend can ever show it."""
    from agentco import db, events
    from agentco.mcp_server import _LocalBackend

    client = make(tmp_path)
    call(client, "POST", "/memory", {"label": {"owner": "acme", "level": "all"}, "layer": "shared",
                                      "title": "t", "body": "b", "provenance": {"kind": "run", "ref": "r"}})
    page = call(client, "GET", "/events").json()
    assert [e for e in page["events"] if e["kind"] in events.WALLED_KINDS] == []
    conn = db.connect(tmp_path / "api.sqlite3")
    assert conn.execute("SELECT COUNT(*) AS n FROM events WHERE kind = 'MemoryChanged'").fetchone()["n"] == 0
    monkeypatch.setenv("AGENTCO_HUB_CLEARANCE", "acme:private")
    local = _LocalBackend(conn, None, None, "dana", str(tmp_path / "api.sqlite3"))
    assert [e for e in local.events(limit=500)["events"] if e["kind"] == "MemoryChanged"] == []
