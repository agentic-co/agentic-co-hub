"""The ready-work read is filtered by the agent the SIGNATURE names (ADR 0008 §6).

A read credential that could pick its own agent could read what is ready, and
the assignments and gate metadata that come with it, for anyone.
"""

from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from agentco import auth, authz
from agentco.app import create_app

KEYS = {"dana": "dana-secret", "kofi": "kofi-secret", "operator": "op-secret", "relay": "relay-secret"}


@pytest.fixture()
def client(tmp_path):
    return TestClient(create_app(
        db_path=str(tmp_path / "api.sqlite3"), keys=KEYS, operator="operator",
        work_store=str(tmp_path / "work.jsonl"), sop_store=str(tmp_path / "sops.jsonl"),
        actor_routes={"relay": ["events"]}))


def call(client, actor, method, path, body=None, query=""):
    raw = json.dumps(body).encode() if body is not None else b""
    ts = str(int(time.time()))
    headers = {"X-AgentCo-Actor": actor, "X-AgentCo-Timestamp": ts, "Content-Type": "application/json",
               "X-AgentCo-Signature": auth.sign(KEYS[actor], method, path, ts, raw, query=query)}
    return client.request(method, path + (f"?{query}" if query else ""), content=raw, headers=headers)


@pytest.fixture()
def filed(client):
    ids = {}
    for name, assignee in (("open", None), ("for-dana", "dana"), ("for-kofi", "kofi")):
        body = {"title": name, **({"assignedAgent": assignee} if assignee else {})}
        ids[name] = call(client, "operator", "POST", "/work", body).json()["item"]["id"]
    return ids


def titles(response):
    assert response.status_code == 200, response.text
    return {i["title"] for i in response.json()["items"]}


def test_the_ready_read_is_the_signers_view(client, filed):
    assert titles(call(client, "dana", "GET", "/work", query="ready=1")) == {"open", "for-dana"}
    assert titles(call(client, "kofi", "GET", "/work", query="ready=1")) == {"open", "for-kofi"}


def test_naming_oneself_is_accepted_and_naming_another_is_refused(client, filed):
    assert titles(call(client, "dana", "GET", "/work", query="ready=1&agent=dana")) == {"open", "for-dana"}
    refused = call(client, "dana", "GET", "/work", query="ready=1&agent=kofi")
    assert refused.status_code == 400 and refused.json()["code"] == "agent_from_signature"
    # a plain list has no agent filter, but an agent parameter is the same ask
    refused = call(client, "dana", "GET", "/work", query="agent=kofi")
    assert refused.status_code == 400 and refused.json()["code"] == "agent_from_signature"


def test_the_declared_operator_may_name_any_agent_or_none(client, filed):
    assert titles(call(client, "operator", "GET", "/work", query="ready=1")) == {"open", "for-dana", "for-kofi"}
    assert titles(call(client, "operator", "GET", "/work", query="ready=1&agent=kofi")) == {"open", "for-kofi"}


def test_the_exception_is_the_operator_only_not_a_human_or_an_admin(tmp_path):
    assert authz.read_agent("operator", "x", "operator") == "x"
    with pytest.raises(Exception) as err:
        authz.read_agent("dana", "x", "operator")
    assert getattr(err.value, "code", "") == "agent_from_signature"


def test_a_transport_credential_cannot_read_results_or_gates_through_work(client, filed):
    """The review's probe: a relay reading every item's result and gate
    metadata from `GET /work`. It cannot reach the route at all."""
    for query in ("", "ready=1", "status=pending"):
        refused = call(client, "relay", "GET", "/work", query=query)
        assert refused.status_code == 403 and refused.json()["code"] == "route_not_allowed", query
