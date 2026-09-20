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

from agentco import auth, db, delivery, digests, events, snapshots
from agentco.app import create_app
from agentco.cli import build_parser, cmd_digest
from agentco.errors import Refusal
from agentco.publish import Registry

KEYS = {"team-frontend": "team-frontend-secret", "operator": "op-secret"}


@pytest.fixture()
def conn(tmp_path):
    return db.connect(tmp_path / "registry.sqlite3")


@pytest.fixture()
def client(tmp_path):
    app = create_app(
        db_path=str(tmp_path / "api.sqlite3"),
        keys=KEYS,
        operator="operator",
        federated_children=["team-frontend"],
    )
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


FEDERATORS = frozenset({"team-frontend"})


def test_receive_appends_a_digest_received_event(conn):
    receipt = digests.receive(
        conn, actor="team-frontend", text="3 scopes closed, 0 stale", declared_federators=FEDERATORS
    )
    assert receipt["state"] == "accepted"
    assert receipt["eventId"].startswith("evt_")

    row = conn.execute("SELECT * FROM events WHERE uid = ?", (receipt["eventId"],)).fetchone()
    assert row["kind"] == "DigestReceived"
    assert row["actor"] == "team-frontend"
    payload = json.loads(row["payload"])
    assert payload["text"] == "3 scopes closed, 0 stale"


def test_an_empty_digest_is_refused_not_recorded(conn):
    with pytest.raises(Refusal) as exc:
        digests.receive(
            conn, actor="team-frontend", text="   ", declared_federators=FEDERATORS
        )
    assert exc.value.code == "text_required"


def test_a_non_string_digest_is_refused_not_a_500(conn):
    for bad in (5, ["a"], {"a": 1}):
        with pytest.raises(Refusal) as exc:
            digests.receive(
                conn, actor="team-frontend", text=bad, declared_federators=FEDERATORS
            )
        assert exc.value.code == "text_required"


def test_a_non_object_meta_is_refused(conn):
    with pytest.raises(Refusal) as exc:
        digests.receive(
            conn,
            actor="team-frontend",
            text="hello",
            meta="not an object",
            declared_federators=FEDERATORS,
        )
    assert exc.value.code == "meta_must_be_object"


def test_digest_received_is_a_real_kind_not_a_typo():
    # A closed-set violation raises ValueError from events.append itself —
    # this just pins that the kind this module writes is one events.py knows.
    assert "DigestReceived" in events.KINDS


# --------------------------------------------------------------------------- #
# The gap both cross-vendor reviews converged on: nothing distinguished a
# declared child hub from an ordinary correctly-signed actor. Fails CLOSED —
# see digests.py's module docstring for why that (not humans/verifiers'
# fail-open) is the right default here.
# --------------------------------------------------------------------------- #


def test_an_undeclared_actor_is_refused_even_though_the_signature_is_valid(conn):
    with pytest.raises(Refusal) as exc:
        digests.receive(conn, actor="team-frontend", text="hello", declared_federators=frozenset())
    assert exc.value.code == "not_a_declared_federator"


def test_federation_is_off_by_default_when_nothing_is_declared(conn, monkeypatch):
    for var in (digests.FEDERATORS_ENV_VAR, digests.LEGACY_FEDERATORS_ENV_VAR):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(Refusal) as exc:
        digests.receive(conn, actor="team-frontend", text="hello")
    assert exc.value.code == "not_a_declared_federator"


def test_an_endpoint_level_undeclared_actor_is_refused(tmp_path):
    """The same fail-closed rule at the transport, not just the domain
    function — a hub with NO federated_children declared refuses everyone,
    even a correctly-signed actor a sibling deployment might trust."""
    app = create_app(
        db_path=str(tmp_path / "api.sqlite3"), keys=KEYS, operator="operator"
    )  # no federated_children passed — undeclared
    c = TestClient(app)
    response = post(c, "/digests", "team-frontend", {"text": "hello"})
    assert response.status_code == 422
    assert response.json()["code"] == "not_a_declared_federator"


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

    app = create_app(
        db_path=str(tmp_path / "parent.sqlite3"),
        keys=KEYS,
        operator="operator",
        federated_children=["team-frontend"],
    )
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


def test_post_to_hub_wraps_a_non_json_200_as_delivery_failed(monkeypatch):
    """The likeliest real misconfiguration — a proxy, an SSO portal, a wrong
    path — answers 200 with an HTML body, not JSON. Must surface as
    `DeliveryFailed`, never a bare `json.JSONDecodeError` the caller's
    `except (DeliveryNotConfigured, DeliveryFailed)` would not catch."""
    import http.server
    import threading

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 - stdlib method name
            length = int(self.headers.get("Content-Length", 0))
            self.rfile.read(length)
            body = b"<html>not json</html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):  # silence
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        monkeypatch.setenv(delivery.HUB_URL_ENV_VAR, f"http://127.0.0.1:{server.server_port}")
        monkeypatch.setenv(delivery.HUB_ACTOR_ENV_VAR, "team-frontend")
        monkeypatch.setenv(delivery.HUB_SECRET_ENV_VAR, "whatever")

        with pytest.raises(delivery.DeliveryFailed) as exc:
            delivery.send("text", {}, via="hub")
        assert "non-JSON" in str(exc.value) or "non-JSON" in exc.value.detail
    finally:
        server.shutdown()
        thread.join(timeout=5)


# --------------------------------------------------------------------------- #
# `cmd_digest`'s delivery ordering — found in review: `divergence.deliver`
# (which marks every moved pointer "said once") ran BEFORE `delivery.send`,
# so a failed `--post` still lost that pointer from every future digest.
# --------------------------------------------------------------------------- #


def _digest_args(db_path, tmp_path, via="hub"):
    return build_parser().parse_args(
        ["--db", str(db_path), "digest", "--deliver", "--post", "--via", via]
    )


def _moved_count(db_path) -> int:
    conn = db.connect(db_path)
    return conn.execute(
        "SELECT COUNT(*) AS n FROM events WHERE kind = 'DivergenceObserved'"
    ).fetchone()["n"]


def test_a_failed_post_marks_nothing_delivered(tmp_path, monkeypatch, capsys):
    db_path = tmp_path / "child.sqlite3"
    conn = db.connect(db_path)
    artifact = tmp_path / "prd.md"
    artifact.write_text("v1")
    snapshots.take(conn, actor="dana", artifact_uri=f"file:{artifact}", purpose="baseline")
    artifact.write_text("v2 — changed")
    conn.close()

    for var in (delivery.HUB_URL_ENV_VAR, delivery.HUB_ACTOR_ENV_VAR, delivery.HUB_SECRET_ENV_VAR):
        monkeypatch.delenv(var, raising=False)  # not configured -> DeliveryNotConfigured

    exit_code = cmd_digest(_digest_args(db_path, tmp_path))
    assert exit_code == 1
    assert _moved_count(db_path) == 0, (
        "the moved pointer must stay unmarked so the NEXT digest still reports it — "
        "a failed send that marks delivery anyway is a permanent, silent hole"
    )


def test_a_successful_post_marks_delivered(tmp_path, monkeypatch, live_hub):
    url, _ = live_hub
    db_path = tmp_path / "child.sqlite3"
    conn = db.connect(db_path)
    artifact = tmp_path / "prd.md"
    artifact.write_text("v1")
    snapshots.take(conn, actor="team-frontend", artifact_uri=f"file:{artifact}", purpose="baseline")
    artifact.write_text("v2 — changed")
    conn.close()

    monkeypatch.setenv(delivery.HUB_URL_ENV_VAR, url)
    monkeypatch.setenv(delivery.HUB_ACTOR_ENV_VAR, "team-frontend")
    monkeypatch.setenv(delivery.HUB_SECRET_ENV_VAR, KEYS["team-frontend"])

    exit_code = cmd_digest(_digest_args(db_path, tmp_path))
    assert exit_code == 0
    assert _moved_count(db_path) == 1


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
