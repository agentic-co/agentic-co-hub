"""Wire defaults that change at 1.0.

Known issue 6b: a signature over the OLD string, without the canonical query line,
let one captured `GET /events` replay as any feed query for the replay window. The
fix shipped switchable and OFF; at 1.0 the safe behaviour is the default.

Semantics under test (`auth.legacy_signature_allowed`):

  * variable unset, empty, or anything unrecognised: the signed query is REQUIRED
    (fail safe: a typo in the variable must never re-open the hole);
  * `0`, `false`, `no`, `off`, case-insensitive, surrounding whitespace ignored:
    the legacy signature is accepted again. One-release opt-out for an operator
    whose clients have not moved.

`tests/test_replay_window.py` shows the replay itself; this file pins the default.
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


def legacy_get(client, path="/events", query="limit=50"):
    ts = str(int(time.time()))
    headers = {"X-AgentCo-Actor": "dana", "X-AgentCo-Timestamp": ts,
               "X-AgentCo-Signature": auth.sign(KEYS["dana"], "GET", path, ts, b"")}
    return client.get(f"{path}?{query}", headers=headers)


def test_a_legacy_signature_on_a_get_with_a_query_is_refused_by_default(client, monkeypatch):
    monkeypatch.delenv(auth.REQUIRE_SIGNED_QUERY_ENV_VAR, raising=False)
    response = legacy_get(client)
    assert response.status_code == 401, "an unset switch accepted a signature that does not cover the query"
    assert response.json()["code"] == "unauthenticated"


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "FALSE", "Off", " no ", "No"])
def test_an_explicit_falsy_value_re_allows_the_legacy_signature(client, monkeypatch, value):
    monkeypatch.setenv(auth.REQUIRE_SIGNED_QUERY_ENV_VAR, value)
    assert legacy_get(client).status_code == 200


@pytest.mark.parametrize("value", ["", " ", "1", "true", "yes", "on", "maybe", "disabled", "00", "flase", "2"])
def test_anything_that_is_not_an_explicit_opt_out_keeps_the_query_required(client, monkeypatch, value):
    """Fail safe: a typo in the variable must never re-open 6b."""
    monkeypatch.setenv(auth.REQUIRE_SIGNED_QUERY_ENV_VAR, value)
    assert legacy_get(client).status_code == 401


def test_the_opt_out_does_not_weaken_a_signed_query(client, monkeypatch):
    """The current form works under every setting, or the switch would be an outage."""
    for value in (None, "0", "1"):
        if value is None:
            monkeypatch.delenv(auth.REQUIRE_SIGNED_QUERY_ENV_VAR, raising=False)
        else:
            monkeypatch.setenv(auth.REQUIRE_SIGNED_QUERY_ENV_VAR, value)
        ts = str(int(time.time()))
        headers = {"X-AgentCo-Actor": "dana", "X-AgentCo-Timestamp": ts,
                   "X-AgentCo-Signature": auth.sign(KEYS["dana"], "GET", "/events", ts, b"", query="limit=50")}
        assert client.get("/events?limit=50", headers=headers).status_code == 200


def test_the_opt_out_does_not_let_a_different_query_replay_a_signed_one(client, monkeypatch):
    """Opting out re-allows the OLD string, not a mismatched new one."""
    monkeypatch.setenv(auth.REQUIRE_SIGNED_QUERY_ENV_VAR, "0")
    ts = str(int(time.time()))
    headers = {"X-AgentCo-Actor": "dana", "X-AgentCo-Timestamp": ts,
               "X-AgentCo-Signature": auth.sign(KEYS["dana"], "GET", "/events", ts, b"", query="limit=1")}
    assert client.get("/events?limit=500", headers=headers).status_code == 401
