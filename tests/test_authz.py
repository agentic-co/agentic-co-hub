"""The per-actor route allowlist (ADR 0008 §3.3): a credential that reaches
only the routes its key-table entry names.

What would fail if the feature were dishonest: a restricted actor reaching a
route it was not given; an edit to the table needing a restart; a malformed
entry failing OPEN; the refusal arriving after the body was parsed; a route
added to `app.py` that nobody decided how to allowlist.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agentco import auth, authz
from agentco.app import create_app

SECRETS = {"dana": "dana-secret", "operator": "op-secret", "relay": "relay-secret"}


def _write(path: Path, relay_entry) -> None:
    table = {"dana": SECRETS["dana"], "operator": SECRETS["operator"]}
    table["relay"] = relay_entry if isinstance(relay_entry, str) else {"secret": SECRETS["relay"], **relay_entry}
    path.write_text(json.dumps(table))
    path.chmod(0o600)


@pytest.fixture()
def keyfile(tmp_path):
    path = tmp_path / "keys.json"
    _write(path, {"routes": ["events", "attest"]})
    return path


@pytest.fixture()
def client(tmp_path, keyfile, monkeypatch):
    monkeypatch.setenv(auth.KEYS_ENV_VAR, str(keyfile))
    return TestClient(create_app(
        db_path=str(tmp_path / "api.sqlite3"), operator="operator",
        work_store=str(tmp_path / "work.jsonl"), sop_store=str(tmp_path / "sops.jsonl")))


def call(client, actor, method, path, body=None, query="", raw=None):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else b"")
    ts = str(int(time.time()))
    headers = {"X-AgentCo-Actor": actor, "X-AgentCo-Timestamp": ts, "Content-Type": "application/json",
               "X-AgentCo-Signature": auth.sign(SECRETS[actor], method, path, ts, data, query=query)}
    return client.request(method, path + (f"?{query}" if query else ""), content=data, headers=headers)


def test_every_verb_in_app_is_known_to_the_allowlist():
    """Both directions: a route added without a decision here fails CI, and a
    verb listed here that no route uses is a name that grants nothing."""
    source = (Path(__file__).resolve().parents[1] / "agentco" / "app.py").read_text()
    used = set(re.findall(r'_handle\(request, "([a-z_]+)"', source))
    assert used, "the pattern found no verbs; the test would pass vacuously"
    assert used == set(authz.VERBS), (sorted(used - authz.VERBS), sorted(authz.VERBS - used))


def test_a_restricted_actor_reaches_only_its_routes(client):
    assert call(client, "relay", "GET", "/events").status_code == 200
    for method, path, body in (("GET", "/work", None), ("POST", "/work", {"title": "t"}),
                               ("POST", "/work/pull", {}), ("GET", "/sops", None)):
        refused = call(client, "relay", method, path, body)
        assert refused.status_code == 403 and refused.json()["code"] == "route_not_allowed", (path, refused.text)
        assert "routes" in refused.json()["remediation"]
    # an allowed verb is reached: the handler answers, not the allowlist
    reached = call(client, "relay", "POST", "/work/nope/attest", {"attempt": 1, "attestation": {}})
    assert reached.json().get("code") != "route_not_allowed"


def test_an_unrestricted_actor_is_unaffected(client):
    assert call(client, "dana", "GET", "/work").status_code == 200
    assert call(client, "operator", "GET", "/events").status_code == 200


def test_the_refusal_comes_before_the_body_is_read(client):
    """A restricted credential must not learn whether its body parses."""
    refused = call(client, "relay", "POST", "/work", raw=b"{not json")
    assert refused.status_code == 403 and refused.json()["code"] == "route_not_allowed"


def test_an_edit_applies_on_the_next_request(client, keyfile):
    assert call(client, "relay", "GET", "/events").status_code == 200
    _write(keyfile, {"routes": ["attest"]})
    assert call(client, "relay", "GET", "/events").json()["code"] == "route_not_allowed"
    _write(keyfile, {})  # routes removed: unrestricted again
    assert call(client, "relay", "GET", "/work").status_code == 200


@pytest.mark.parametrize("routes", [[], "events", {"events": True}, [1], ["events", None], "attest"])
def test_an_empty_or_malformed_list_reaches_nothing(client, keyfile, routes):
    """Fails CLOSED for that actor, and only that actor."""
    _write(keyfile, {"routes": routes})
    assert call(client, "relay", "GET", "/events").json()["code"] == "route_not_allowed"
    assert call(client, "dana", "GET", "/events").status_code == 200


def test_an_unknown_name_grants_nothing(client, keyfile):
    _write(keyfile, {"routes": ["evnets", "attest"]})
    assert call(client, "relay", "GET", "/events").json()["code"] == "route_not_allowed"


def test_the_transport_group_reaches_the_answer_route_and_receipts_only(client, keyfile):
    assert authz.PROFILES["transport"] == frozenset({"answer_submit", "answer_receipt"})
    _write(keyfile, {"routes": ["transport"]})
    assert call(client, "relay", "GET", "/events").json()["code"] == "route_not_allowed"
    _write(keyfile, {"routes": ["transport", "events"]})
    assert call(client, "relay", "GET", "/events").status_code == 200


def test_a_malformed_entry_does_not_break_signing_for_everyone(client, keyfile):
    _write(keyfile, {"routes": 7})
    assert call(client, "dana", "GET", "/events").status_code == 200


def test_injected_routes_win_and_refusals_are_recorded(tmp_path):
    app = create_app(db_path=str(tmp_path / "a.sqlite3"), keys=SECRETS, operator="operator",
                     work_store=str(tmp_path / "w.jsonl"), sop_store=str(tmp_path / "s.jsonl"),
                     actor_routes={"relay": ["events"]})
    c = TestClient(app)
    assert call(c, "relay", "GET", "/events").status_code == 200
    assert call(c, "relay", "GET", "/work").json()["code"] == "route_not_allowed"
    metrics = call(c, "dana", "GET", "/metrics").json()
    assert "route_not_allowed" in json.dumps(metrics)


def test_an_unsigned_query_is_refused_when_the_hub_requires_signed_queries(client, monkeypatch):
    monkeypatch.setenv(auth.REQUIRE_SIGNED_QUERY_ENV_VAR, "1")
    ts = str(int(time.time()))
    unsigned = {"X-AgentCo-Actor": "dana", "X-AgentCo-Timestamp": ts,
                "X-AgentCo-Signature": auth.sign(SECRETS["dana"], "GET", "/work", ts, b"")}
    assert client.get("/work?ready=1", headers=unsigned).status_code == 401
    assert call(client, "dana", "GET", "/work", query="ready=1").status_code == 200
