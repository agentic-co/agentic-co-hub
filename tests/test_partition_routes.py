"""The partition transport credential class (ADR 0008 §8 B1/B2).

What would fail if the class were dishonest: a relay reading an unassigned item
or someone else's; a hidden item answering differently from a missing one; the
feed leaking a non-key kind; an ownerless transport entry loading; a widening
query parameter being ignored instead of refused.
"""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.request

import pytest
from fastapi.testclient import TestClient

from agentco import auth, authz, events
from agentco.app import create_app

KEYS = {"dana": "dana-secret", "kofi": "kofi-secret", "operator": "op-secret",
        "dana-bot": "bot-secret", "relay": "relay-secret"}
TABLE = {
    "dana": KEYS["dana"], "kofi": KEYS["kofi"], "operator": KEYS["operator"],
    "dana-bot": {"secret": KEYS["dana-bot"], "owner": "dana"},
    "relay": {"secret": KEYS["relay"], "owner": "dana", "class": "transport"},
}


def sign_headers(actor, method, path, body=b"", query=""):
    ts = str(int(time.time()))
    return {"X-AgentCo-Actor": actor, "X-AgentCo-Timestamp": ts, "Content-Type": "application/json",
            "X-AgentCo-Signature": auth.sign(KEYS[actor], method, path, ts, body, query=query)}


def call(client, actor, method, path, body=None, query=""):
    raw = json.dumps(body).encode() if body is not None else b""
    return client.request(method, path + (f"?{query}" if query else ""), content=raw,
                          headers=sign_headers(actor, method, path, raw, query))


@pytest.fixture()
def keyfile(tmp_path, monkeypatch):
    path = tmp_path / "keys.json"
    path.write_text(json.dumps(TABLE))
    path.chmod(0o600)
    monkeypatch.setenv(auth.KEYS_ENV_VAR, str(path))
    return path


@pytest.fixture()
def app(tmp_path, keyfile):
    return create_app(db_path=str(tmp_path / "api.sqlite3"), operator="operator", hub_id="hub-one",
                      work_store=str(tmp_path / "work.jsonl"), sop_store=str(tmp_path / "sops.jsonl"))


@pytest.fixture()
def client(app):
    return TestClient(app)


@pytest.fixture()
def filed(client):
    """open (unassigned), dana's, dana-bot's, kofi's, a vehicle for dana verifying `subject`."""
    ids = {}

    def file(name, **extra):
        out = call(client, "operator", "POST", "/work", {"title": name, **extra}).json()
        ids[name] = out["item"]["id"]

    file("open")
    file("for-dana", assignedAgent="dana")
    file("for-bot", assignedAgent="dana-bot")
    file("for-kofi", assignedAgent="kofi")
    file("subject", assignedAgent="kofi")
    file("other-subject", assignedAgent="kofi")
    return ids


def vehicle_for(app, ids, name, subject, assignee):
    """The plane files vehicles; no route lets a caller set `verifies`, so the test writes through the store."""
    made = app.state.queue.create(title=name, assigned_agent=assignee, metadata={"verifies": ids[subject]}, by_plane=True)
    ids[name] = made.id
    return made


def titles(response):
    assert response.status_code == 200, response.text
    return {i["title"] for i in response.json()["items"]}


# -- config ----------------------------------------------------------------

def test_a_transport_entry_without_an_owner_is_not_loaded(tmp_path):
    path = tmp_path / "k.json"
    path.write_text(json.dumps({"dana": "s1", "relay": {"secret": "s2", "class": "transport"}}))
    assert set(auth.load_keys(path)) == {"dana"}


def test_an_unknown_class_is_not_loaded(tmp_path):
    path = tmp_path / "k.json"
    path.write_text(json.dumps({"dana": "s1", "x": {"secret": "s2", "owner": "dana", "class": "transprot"}}))
    assert set(auth.load_keys(path)) == {"dana"}


def test_a_transport_cannot_be_its_own_owner(tmp_path):
    path = tmp_path / "k.json"
    path.write_text(json.dumps({"relay": {"secret": "s2", "owner": "relay", "class": "transport"}}))
    with pytest.raises(auth.AmbiguousIdentityError):
        auth.load_keys(path)


def test_the_class_parses_with_its_owner(tmp_path):
    path = tmp_path / "k.json"
    path.write_text(json.dumps(TABLE))
    ident = auth.load_identities(path)["relay"]
    assert (ident.klass, ident.owner) == ("transport", "dana")
    assert auth.load_identities(path)["dana-bot"].klass is None


def test_the_key_table_grants_the_class_verbs_and_routes_only_narrow(tmp_path, monkeypatch):
    path = tmp_path / "k.json"
    table = dict(TABLE)
    table["relay"] = {**TABLE["relay"], "routes": ["events", "work_create", "attest"]}
    path.write_text(json.dumps(table))
    monkeypatch.setenv(auth.KEYS_ENV_VAR, str(path))
    assert authz.actors_from_key_table()["relay"] == frozenset({"events"})
    table["relay"] = TABLE["relay"]
    path.write_text(json.dumps(table))
    assert authz.actors_from_key_table()["relay"] == authz.TRANSPORT_VERBS
    table["relay"] = {"secret": "s", "owner": "dana", "class": "bogus"}
    path.write_text(json.dumps(table))
    assert authz.actors_from_key_table()["relay"] == frozenset()


def test_every_transport_verb_is_a_known_verb():
    assert authz.TRANSPORT_VERBS <= authz.VERBS


# -- GET /hub --------------------------------------------------------------

def test_the_hub_route_answers_every_actor_including_the_transport(client, app):
    for actor in ("dana", "relay", "operator"):
        got = call(client, actor, "GET", "/hub")
        assert got.status_code == 200, got.text
        body = got.json()
        assert body["hubId"] == app.state.hub_uid and re.fullmatch(r"[0-9a-f]{32}", body["hubId"])
        assert body["label"] == "hub-one"


# -- GET /work?ready=1 -----------------------------------------------------

def test_the_ready_read_is_the_partys_items_only(client, filed):
    got = titles(call(client, "relay", "GET", "/work", query="ready=1"))
    assert got == {"for-dana", "for-bot"}  # the party and an actor the party owns
    assert "open" not in got and "for-kofi" not in got  # unassigned and another party's


@pytest.mark.parametrize("query", ["", "status=pending"])
def test_the_unfiltered_list_is_refused(client, filed, query):
    refused = call(client, "relay", "GET", "/work", query=query)
    assert refused.status_code == 403 and refused.json()["code"] == "route_not_allowed"


@pytest.mark.parametrize("query", ["ready=1&agent=kofi", "ready=1&agent=dana", "ready=1&status=pending", "ready=1&x=1"])
def test_a_widening_parameter_is_refused_not_ignored(client, filed, query):
    refused = call(client, "relay", "GET", "/work", query=query)
    assert refused.status_code == 400 and refused.json()["code"] == "transport_query_refused", query


def test_other_actors_ready_reads_are_unchanged(client, filed):
    assert titles(call(client, "kofi", "GET", "/work", query="ready=1")) == {"open", "for-kofi", "subject", "other-subject"}


# -- GET /work/{id} --------------------------------------------------------

def test_the_single_item_read_for_an_ordinary_actor(client, filed):
    got = call(client, "kofi", "GET", f"/work/{filed['open']}")
    assert got.status_code == 200 and got.json()["item"]["title"] == "open"
    missing = call(client, "kofi", "GET", "/work/no-such-item")
    assert missing.status_code == 404 and missing.json()["code"] == "work_item_unknown"


def test_the_transport_reads_its_vehicle_and_what_it_verifies_and_nothing_else(client, app, filed):
    vehicle = vehicle_for(app, filed, "vehicle", "subject", "dana")
    assert call(client, "relay", "GET", f"/work/{vehicle.id}").json()["item"]["title"] == "vehicle"
    assert call(client, "relay", "GET", f"/work/{filed['subject']}").json()["item"]["title"] == "subject"
    for name in ("open", "for-dana", "for-kofi", "other-subject"):
        got = call(client, "relay", "GET", f"/work/{filed[name]}")
        assert got.status_code == 404 and got.json()["code"] == "work_item_unknown", name


def test_a_vehicle_of_another_party_is_hidden(client, app, filed):
    vehicle = vehicle_for(app, filed, "kofis-vehicle", "other-subject", "kofi")
    assert call(client, "relay", "GET", f"/work/{vehicle.id}").status_code == 404
    assert call(client, "relay", "GET", f"/work/{filed['other-subject']}").status_code == 404


def test_hidden_and_missing_answer_identically_apart_from_the_id(client, app, filed):
    hidden = call(client, "relay", "GET", f"/work/{filed['open']}")
    missing = call(client, "relay", "GET", "/work/ghost-id")
    assert hidden.status_code == missing.status_code == 404
    norm = lambda r, i: json.dumps(r.json(), sort_keys=True).replace(i, "<id>")  # noqa: E731
    assert norm(hidden, filed["open"]) == norm(missing, "ghost-id")
    assert set(hidden.headers) - {"content-length"} == set(missing.headers) - {"content-length"}


# -- GET /events -----------------------------------------------------------

def _emit(app, kind, payload=None):
    events.announcer(app.state.conn)(kind, "someone", payload or {})


def test_the_feed_for_a_transport_is_human_key_kinds_only(client, app, filed):
    _emit(app, "HumanKeyEnrolled", {"who": "dana"})
    _emit(app, "ScopeClaimed", {"repo": "r"})
    _emit(app, "HumanKeyRevoked", {"who": "dana"})
    got = call(client, "relay", "GET", "/events").json()
    assert [e["kind"] for e in got["events"]] == ["HumanKeyEnrolled", "HumanKeyRevoked"]
    assert got["count"] == 2
    # the cursor advanced past the hidden rows: nothing new on a re-poll
    again = call(client, "relay", "GET", "/events", query=f"since={got['nextCursor']}").json()
    assert again["events"] == []
    # naming another kind returns nothing
    assert call(client, "relay", "GET", "/events", query="kind=WorkFiled").json()["events"] == []
    assert call(client, "relay", "GET", "/events", query="kind=HumanKeyRevoked").json()["count"] == 1
    # an ordinary actor still sees everything
    kinds = {e["kind"] for e in call(client, "kofi", "GET", "/events").json()["events"]}
    assert {"WorkFiled", "ScopeClaimed", "HumanKeyEnrolled"} <= kinds


# -- everything else is refused --------------------------------------------

@pytest.mark.parametrize("method,path,body", [
    ("POST", "/work/some-id/attest", {"attempt": 1, "attestation": {}}),
    ("POST", "/work", {"title": "x"}), ("POST", "/work/pull", {}),
    ("GET", "/runs", None), ("GET", "/sops", None), ("GET", "/memory", None),
    ("GET", "/metrics", None), ("POST", "/digests", {"text": "t"}),
])
def test_every_other_route_is_refused(client, method, path, body):
    refused = call(client, "relay", method, path, body)
    assert refused.status_code == 403 and refused.json()["code"] == "route_not_allowed", (path, refused.text)


def test_injected_transports_work_like_the_key_table(tmp_path):
    app = create_app(db_path=str(tmp_path / "a.sqlite3"), keys=KEYS, operator="operator",
                     work_store=str(tmp_path / "w.jsonl"), sop_store=str(tmp_path / "s.jsonl"),
                     transports={"relay": "dana"})
    c = TestClient(app)
    assert call(c, "relay", "GET", "/hub").status_code == 200
    assert call(c, "relay", "GET", "/work").json()["code"] == "route_not_allowed"


# -- a live socket ---------------------------------------------------------

def test_over_a_live_socket(tmp_path, keyfile):
    import socket

    import uvicorn

    app = create_app(db_path=str(tmp_path / "live.sqlite3"), operator="operator",
                     work_store=str(tmp_path / "lw.jsonl"), sop_store=str(tmp_path / "ls.jsonl"))
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.time() + 10
        while not server.started and time.time() < deadline:
            time.sleep(0.05)
        assert server.started

        def get(path, query=""):
            req = urllib.request.Request(f"http://127.0.0.1:{port}{path}" + (f"?{query}" if query else ""),
                                         headers=sign_headers("relay", "GET", path, b"", query))
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    return r.status, json.loads(r.read())
            except urllib.error.HTTPError as exc:
                return exc.code, json.loads(exc.read())

        status, hub = get("/hub")
        assert status == 200 and re.fullmatch(r"[0-9a-f]{32}", hub["hubId"])
        assert get("/work", "ready=1")[0] == 200
        status, refused = get("/work")
        assert status == 403 and refused["code"] == "route_not_allowed"
        assert get("/work/nope")[0] == 404
        assert get("/events")[0] == 200
        assert get("/sops")[0] == 403
    finally:
        server.should_exit = True
        thread.join(timeout=10)
