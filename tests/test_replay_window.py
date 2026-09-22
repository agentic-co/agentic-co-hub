"""A captured request cannot be replayed as a different one.

Known issue 6b: the signature covered the path but not the query string, so one
captured `GET /events` replayed as ANY feed query for the whole 300-second
window — a different cursor, a different limit, a different kind. The headers
are the same bytes; only the URL changes, and nothing noticed.

Signing a CANONICAL query closes it without giving up the property the omission
was protecting: a proxy that reorders or re-encodes parameters still must not
invalidate a signature.

The transition is deliberate and is tested as such. Both ends have to change
together, so the legacy string is accepted until an operator sets
ASOP_REQUIRE_SIGNED_QUERY — and while it is accepted, 6b is open for whoever is
still signing the old way. That is stated rather than implied, and the test
below shows exactly what the switch changes.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from agentco import auth
from agentco.app import create_app

KEYS = {"dana": "dana-secret", "operator": "op-secret"}


@pytest.fixture()
def client(tmp_path):
    return TestClient(create_app(
        db_path=str(tmp_path / "api.sqlite3"), keys=KEYS, operator="operator",
        work_store=str(tmp_path / "work.jsonl"), sop_store=str(tmp_path / "sops.jsonl"),
    ))


def headers_for(query: str, actor: str = "dana"):
    ts = str(int(time.time()))
    return {"X-AgentCo-Actor": actor, "X-AgentCo-Timestamp": ts,
            "X-AgentCo-Signature": auth.sign(KEYS[actor], "GET", "/events", ts, b"", query=query)}


def test_a_captured_request_cannot_be_replayed_with_a_different_query(client):
    """The attack, in three lines: capture one signed feed read, change the
    cursor, send the same headers."""
    captured = headers_for("?limit=1")
    assert client.get("/events?limit=1", headers=captured).status_code == 200

    replayed = client.get("/events?limit=500", headers=captured)
    assert replayed.status_code == 401, "the captured signature authorised a different query"
    assert replayed.json()["code"] == "unauthenticated"


def test_reordering_the_same_parameters_still_works(client):
    """The property the omission was protecting. A proxy is allowed to reorder."""
    signed = headers_for("?kind=WorkFiled&limit=50")
    assert client.get("/events?limit=50&kind=WorkFiled", headers=signed).status_code == 200


def test_a_legacy_signature_is_accepted_during_the_transition(client):
    """Both ends change together, and a registry that stopped serving every
    existing client on upgrade is a registry nobody upgrades."""
    ts = str(int(time.time()))
    legacy = {"X-AgentCo-Actor": "dana", "X-AgentCo-Timestamp": ts,
              "X-AgentCo-Signature": auth.sign(KEYS["dana"], "GET", "/events", ts, b"")}
    assert client.get("/events?limit=50", headers=legacy).status_code == 200


def test_the_switch_closes_it(client, monkeypatch):
    """And this is 6b actually shut: once every client has moved, the operator
    sets the variable and the old string stops being enough."""
    monkeypatch.setenv(auth.REQUIRE_SIGNED_QUERY_ENV_VAR, "1")
    ts = str(int(time.time()))
    legacy = {"X-AgentCo-Actor": "dana", "X-AgentCo-Timestamp": ts,
              "X-AgentCo-Signature": auth.sign(KEYS["dana"], "GET", "/events", ts, b"")}
    assert client.get("/events?limit=50", headers=legacy).status_code == 401

    # and the current form still works, or the switch would just be an outage
    assert client.get("/events?limit=50", headers=headers_for("?limit=50")).status_code == 200


def test_a_request_with_no_query_is_unaffected_either_way(client, monkeypatch):
    """Most of the surface. It must not need a client change at all."""
    for value in (None, "1"):
        if value:
            monkeypatch.setenv(auth.REQUIRE_SIGNED_QUERY_ENV_VAR, value)
        ts = str(int(time.time()))
        headers = {"X-AgentCo-Actor": "dana", "X-AgentCo-Timestamp": ts,
                   "X-AgentCo-Signature": auth.sign(KEYS["dana"], "GET", "/events", ts, b"")}
        assert client.get("/events", headers=headers).status_code == 200
