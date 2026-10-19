"""A federated child reads only its own digests (ADR 0005; the control-loop review's G4).

A parent holds one key per declared child. Before this, any child key read every
other child's `DigestReceived` rows, `meta` included, through `GET /events`: only
`MemoryChanged` was filtered on the way out. Now a declared child sees the rows it
sent and no others, on every path that reads the feed (HTTP, a real socket, the MCP
`events` tool locally and over the relay). The parent's own readers, anyone who is
not a declared child, still read every digest. Hidden rows leave the page and
`count`; the cursor still moves past them, as for a walled memory event.

Synthetic children `co-a` and `co-b`; `operator` and `dana` are the parent's own.
"""

from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from agentco import auth, events
from agentco.app import create_app

KEYS = {"co-a": "co-a-secret", "co-b": "co-b-secret", "operator": "op-secret", "dana": "dana-secret"}
CHILDREN = ["co-a", "co-b"]


def make_app(tmp_path):
    return create_app(db_path=str(tmp_path / "api.sqlite3"), keys=KEYS, operator="operator",
                      work_store=str(tmp_path / "work.jsonl"), sop_store=str(tmp_path / "sops.jsonl"),
                      federated_children=CHILDREN)


def http(client, method, path, actor, body=None, query=""):
    raw = json.dumps(body).encode() if body is not None else b""
    ts = str(int(time.time()))
    headers = {"X-AgentCo-Actor": actor, "X-AgentCo-Timestamp": ts,
               "X-AgentCo-Signature": auth.sign(KEYS[actor], method, path, ts, raw, query=query or None),
               "Content-Type": "application/json"}
    return client.request(method, f"{path}?{query}" if query else path, content=raw or None, headers=headers)


def seed(post):
    """Each child files two digests, interleaved, plus one ordinary event."""
    for child, n in (("co-a", 1), ("co-b", 1), ("co-a", 2), ("co-b", 2)):
        post(child, {"text": f"{child} rollup {n}", "meta": {"secretMargin": f"{child}-{n}"}})


def test_a_child_reads_only_its_own_digests_and_the_parent_reads_all(tmp_path):
    client = TestClient(make_app(tmp_path))
    seed(lambda actor, body: http(client, "POST", "/digests", actor, body))
    http(client, "POST", "/scope-claims", "dana",
         {"repo": "acme/app", "prefixes": ["src/api/handlers"], "intent": "implement"})

    def page(actor, query="kind=DigestReceived"):
        response = http(client, "GET", "/events", actor, query=query)
        assert response.status_code == 200, response.text
        return response.json()

    for child, other in (("co-a", "co-b"), ("co-b", "co-a")):
        mine = page(child)
        assert [e["actor"] for e in mine["events"]] == [child, child] and mine["count"] == 2
        assert other not in json.dumps(mine), "nothing of the other child's digest, meta included"
        unfiltered = page(child, "limit=500")
        kinds = [(e["kind"], e["actor"]) for e in unfiltered["events"]]
        assert ("DigestReceived", other) not in kinds and ("ScopeClaimed", "dana") in kinds
        assert unfiltered["nextCursor"] == page("operator", "limit=500")["nextCursor"], "the cursor moves past them"
    for parent in ("operator", "dana"):
        assert [e["actor"] for e in page(parent)["events"]] == ["co-a", "co-b", "co-a", "co-b"]

    # resuming on a cursor stays scoped
    cursor = page("co-a", "kind=DigestReceived&limit=1")["nextCursor"]
    rest = page("co-a", f"kind=DigestReceived&since={cursor}")
    assert [e["payload"]["text"] for e in rest["events"]] == ["co-a rollup 2"]


def test_the_core_read_without_a_reader_is_unchanged(tmp_path):
    """Internal readers (write-back, conformance) name no reader and see all."""
    app = make_app(tmp_path)
    client = TestClient(app)
    seed(lambda actor, body: http(client, "POST", "/digests", actor, body))
    assert len(events.read(app.state.conn, kind="DigestReceived")["events"]) == 4
    assert len(events.read(app.state.conn, kind="DigestReceived", reader="co-a")["events"]) == 4, \
        "a reader is scoped only when it is a declared child"
    assert len(events.read(app.state.conn, kind="DigestReceived", reader="co-a",
                           children=frozenset(CHILDREN))["events"]) == 2


def test_the_local_mcp_events_tool_is_scoped_too(tmp_path, monkeypatch):
    from agentco.mcp_server import _LocalBackend

    app = make_app(tmp_path)
    client = TestClient(app)
    seed(lambda actor, body: http(client, "POST", "/digests", actor, body))
    monkeypatch.setenv("ASOP_FEDERATED_CHILDREN", ",".join(CHILDREN))
    local = _LocalBackend(app.state.conn, None, None, "co-b", str(tmp_path / "api.sqlite3"))
    assert {e["actor"] for e in local.events(kind="DigestReceived")["events"]} == {"co-b"}


def test_live_socket_and_the_mcp_relay(tmp_path):
    import socket
    import threading

    import uvicorn

    from agentco.mcp_server import create_server
    from agentco.publish import Registry

    app = make_app(tmp_path)
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
        url = f"http://127.0.0.1:{port}"
        reg = {a: Registry(a, KEYS[a], url) for a in KEYS}
        seed(lambda actor, body: reg[actor].digest(body["text"], meta=body["meta"]))
        assert {e["actor"] for e in reg["co-a"].events(kind="DigestReceived")["events"]} == {"co-a"}
        assert {e["actor"] for e in reg["operator"].events(kind="DigestReceived")["events"]} == {"co-a", "co-b"}
        relay = create_server(registry=reg["co-b"])
        page = relay._tool_manager.get_tool("events").fn(kind="DigestReceived")
        assert {e["actor"] for e in page["events"]} == {"co-b"} and "co-a-1" not in json.dumps(page)
    finally:
        server.should_exit = True
        thread.join(timeout=5)


@pytest.mark.parametrize("kind", sorted(events.SENDER_SCOPED_KINDS))
def test_every_sender_scoped_kind_is_a_known_kind(kind):
    assert kind in events.KINDS
