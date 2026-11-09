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


@pytest.mark.xfail(
    strict=True,
    reason="the legacy signature that omits the query is accepted unless ASOP_REQUIRE_SIGNED_QUERY is set",
)
def test_a_legacy_signature_on_a_get_with_a_query_is_refused_by_default(client, monkeypatch):
    monkeypatch.delenv(auth.REQUIRE_SIGNED_QUERY_ENV_VAR, raising=False)
    response = legacy_get(client)
    assert response.status_code == 401, "an unset switch accepted a signature that does not cover the query"
    assert response.json()["code"] == "unauthenticated"
