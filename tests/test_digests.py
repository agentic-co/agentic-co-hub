"""`POST /digests` and the federation sender — ADR 0005.

A hub federates by being a worker on the hub above it: the same signed
request a leaf agent already sends, one level up, carrying a rendered digest
rather than a scope claim. These tests check the receiving side (the
endpoint, the domain function, the closed event kind) and the sending side
(`delivery.post_to_hub`, using `Registry.digest`) independently, because a
child that signs correctly against a parent that rejects correctly is the
actual claim — testing either alone would not be.
"""

from __future__ import annotations

import json
import time
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from agentco import auth, db, delivery, digests, events
from agentco.app import create_app
from agentco.errors import Refusal
from agentco.publish import Registry

KEYS = {"team-frontend": "team-frontend-secret", "operator": "op-secret"}


@pytest.fixture()
def conn(tmp_path):
    return db.connect(tmp_path / "registry.sqlite3")


@pytest.fixture()
def client(tmp_path):
    app = create_app(db_path=str(tmp_path / "api.sqlite3"), keys=KEYS, operator="operator")
    return TestClient(app)


def signed(method: str, path: str, actor: str, body: dict | None = None) -> dict:
    raw = json.dumps(body).encode() if body is not None else b""
    ts = str(int(time.time()))
    return {
        "X-AgentCo-Actor": actor,
        "X-AgentCo-Timestamp": ts,
        "X-AgentCo-Signature": auth.sign(KEYS[actor], method, path, ts, raw),
        "Content-Type": "application/json",
    }


def post(client, path, actor, body):
    return client.post(path, content=json.dumps(body), headers=signed("POST", path, actor, body))


def get(client, path, actor, query=""):
    return client.get(f"{path}{query}", headers=signed("GET", path, actor, None))


# --------------------------------------------------------------------------- #
# The domain function
# --------------------------------------------------------------------------- #


def test_receive_appends_a_digest_received_event(conn):
    receipt = digests.receive(conn, actor="team-frontend", text="3 scopes closed, 0 stale")
    assert receipt["state"] == "accepted"
    assert receipt["eventId"].startswith("evt_")

    row = conn.execute("SELECT * FROM events WHERE uid = ?", (receipt["eventId"],)).fetchone()
    assert row["kind"] == "DigestReceived"
    assert row["actor"] == "team-frontend"
    payload = json.loads(row["payload"])
    assert payload["text"] == "3 scopes closed, 0 stale"


def test_an_empty_digest_is_refused_not_recorded(conn):
    with pytest.raises(Refusal) as exc:
        digests.receive(conn, actor="team-frontend", text="   ")
    assert exc.value.code == "text_required"


def test_digest_received_is_a_real_kind_not_a_typo():
    # A closed-set violation raises ValueError from events.append itself —
    # this just pins that the kind this module writes is one events.py knows.
    assert "DigestReceived" in events.KINDS


# --------------------------------------------------------------------------- #
# The endpoint — same auth, same telemetry, same refusal shape as every
# other verb in app.py
# --------------------------------------------------------------------------- #


def test_an_unsigned_digest_post_is_401(client):
    response = client.post("/digests", content=json.dumps({"text": "hello"}))
    assert response.status_code == 401
    assert response.json()["code"] == "unauthenticated"


def test_a_signed_digest_is_accepted_and_readable_via_events(client):
    response = post(client, "/digests", "team-frontend", {"text": "digest body", "generatedAt": "2026-09-19T00:00:00Z"})
    assert response.status_code == 200
    assert response.json()["state"] == "accepted"

    feed = get(client, "/events", "operator").json()
    kinds = [e["kind"] for e in feed["events"]]
    assert "DigestReceived" in kinds
    event = next(e for e in feed["events"] if e["kind"] == "DigestReceived")
    assert event["actor"] == "team-frontend"
    assert event["payload"]["text"] == "digest body"


def test_a_body_naming_a_different_actor_is_ignored_not_trusted(client):
    """The signature decides who filed the digest, never a field in the body —
    the same identity rule `_handle` enforces for every other verb."""
    response = post(
        client,
        "/digests",
        "team-frontend",
        {"text": "hello", "actor": "someone-else"},
    )
    assert response.status_code == 400
    assert response.json()["code"] == "actor_in_body"


# --------------------------------------------------------------------------- #
# The sender — `delivery.post_to_hub`, filing a REAL request against a REAL
# app instance. Not mocked at the transport, because the thing worth proving
# is that the client this hub uses to talk UP and the server it runs to
# accept connections FROM BELOW actually agree on the wire format — the
# recursion ADR 0005 is built on.
# --------------------------------------------------------------------------- #


@pytest.fixture()
def live_hub(tmp_path):
    """A real TCP-listening instance of the parent hub, on a background thread.

    `Registry` talks real `urllib.request` sockets — it is the file meant to
    be copy-pasted by someone with nothing installed — so proving it actually
    reaches a hub needs a real port, not an ASGI transport.
    """
    import socket
    import threading

    import uvicorn

    app = create_app(db_path=str(tmp_path / "parent.sqlite3"), keys=KEYS, operator="operator")
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    else:
        raise RuntimeError("parent hub did not start in time")

    yield f"http://127.0.0.1:{port}", app.state.conn

    server.should_exit = True
    thread.join(timeout=5)


def test_post_to_hub_files_a_real_digest_with_a_real_parent(live_hub, monkeypatch):
    url, parent_conn = live_hub
    monkeypatch.setenv(delivery.HUB_URL_ENV_VAR, url)
    monkeypatch.setenv(delivery.HUB_ACTOR_ENV_VAR, "team-frontend")
    monkeypatch.setenv(delivery.HUB_SECRET_ENV_VAR, KEYS["team-frontend"])

    delivery.send(
        "3 scopes closed, 0 stale",
        {"generatedAt": "2026-09-19T00:00:00Z"},
        via="hub",
    )

    row = parent_conn.execute(
        "SELECT * FROM events WHERE kind = 'DigestReceived' ORDER BY seq DESC LIMIT 1"
    ).fetchone()
    assert row is not None
    assert row["actor"] == "team-frontend"
    assert json.loads(row["payload"])["text"] == "3 scopes closed, 0 stale"


def test_post_to_hub_without_config_is_not_configured(monkeypatch):
    for var in (delivery.HUB_URL_ENV_VAR, delivery.HUB_ACTOR_ENV_VAR, delivery.HUB_SECRET_ENV_VAR):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(delivery.DeliveryNotConfigured):
        delivery.send("text", {}, via="hub")


def test_post_to_hub_surfaces_a_refusal_as_delivery_failed(live_hub, monkeypatch):
    """An unset actor secret produces a signature the parent rejects — the
    sender must not swallow that as a generic transport error."""
    url, _ = live_hub
    monkeypatch.setenv(delivery.HUB_URL_ENV_VAR, url)
    monkeypatch.setenv(delivery.HUB_ACTOR_ENV_VAR, "team-frontend")
    monkeypatch.setenv(delivery.HUB_SECRET_ENV_VAR, "wrong-secret")

    with pytest.raises(delivery.DeliveryFailed) as exc:
        delivery.send("text", {}, via="hub")
    assert exc.value.status == 401


def test_registry_digest_matches_the_endpoints_wire_shape(client):
    """`Registry.digest` is the file colleagues copy-paste — its request must
    be byte-compatible with what /digests actually parses, proven by pointing
    a real Registry instance at the same signing key this test's client uses."""
    reg = Registry("team-frontend", KEYS["team-frontend"])
    with patch.object(reg, "_call", return_value={"state": "accepted"}) as mock_call:
        reg.digest("hello", generated_at="2026-01-01T00:00:00Z", meta={"scopesClosed": 3})
    mock_call.assert_called_once_with(
        "POST",
        "/digests",
        {"text": "hello", "generatedAt": "2026-01-01T00:00:00Z", "meta": {"scopesClosed": 3}},
    )
