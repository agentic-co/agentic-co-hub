"""`GET /events` and the repository spelling.

Scope comparison folds a repository name (`scope.repo_key`: NFC + case-fold), so
`Acme/App` and `acme/app` are one repository to a conflict check. The question
this file settles is whether the change feed treats them the same way.

What the code does today: the feed has NO repository filter. `GET /events` takes
`since`, `limit` and `kind` (`events.read`); `repo` is stored on each event as
the claimant spelled it and echoed back unchanged, and a `repo` query parameter
is not read. So there is nothing to disagree with `repo_key`, and no defect.

The tests pin the two properties that a future repository filter would have to
keep, and are written to accept any honest outcome rather than the one a
reviewer would pick:

  * a client can group the feed by `repo_key` and gets one repository, whatever
    spellings the claimants used (the echo rule in `docs/wire.md`);
  * IF the server honours a `repo` filter, it matches under the same folding —
    `Acme/App` must not hide events recorded for `acme/app`.
"""

from __future__ import annotations

import json
import time
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient

from agentco import auth, scope
from agentco.app import create_app

KEYS = {"dana": "dana-secret", "kofi": "kofi-secret", "operator": "op-secret"}


@pytest.fixture()
def client(tmp_path):
    return TestClient(create_app(
        db_path=str(tmp_path / "api.sqlite3"), keys=KEYS, operator="operator",
        work_store=str(tmp_path / "work.jsonl"), sop_store=str(tmp_path / "sops.jsonl"),
    ))


def _claim(client, actor, repo, prefix):
    body = json.dumps({"repo": repo, "prefixes": [prefix], "intent": "implement"}).encode()
    ts = str(int(time.time()))
    headers = {
        "X-AgentCo-Actor": actor, "X-AgentCo-Timestamp": ts,
        "X-AgentCo-Signature": auth.sign(KEYS[actor], "POST", "/scope-claims", ts, body),
        "Content-Type": "application/json",
    }
    response = client.post("/scope-claims", content=body, headers=headers)
    assert response.status_code == 200, response.text


def _feed(client, **query):
    qs = urlencode(query)
    ts = str(int(time.time()))
    headers = {
        "X-AgentCo-Actor": "operator", "X-AgentCo-Timestamp": ts,
        "X-AgentCo-Signature": auth.sign(
            KEYS["operator"], "GET", "/events", ts, b"", query=auth.canonical_query(qs) or None),
    }
    response = client.get("/events" + (f"?{qs}" if qs else ""), headers=headers)
    assert response.status_code == 200, response.text
    return [e for e in response.json()["events"] if e["kind"] == "ScopeClaimed"]


def test_the_feed_echoes_each_spelling_and_one_repo_key_groups_them(client):
    _claim(client, "dana", "acme/app", "src/billing/ledger")
    _claim(client, "kofi", "Acme/App", "src/search/index")

    claimed = _feed(client)
    assert sorted(e["repo"] for e in claimed) == ["Acme/App", "acme/app"]
    assert {scope.repo_key(e["repo"]) for e in claimed} == {"acme/app"}


def test_a_repo_filter_if_the_server_honours_one_folds_like_scope_comparison(client):
    _claim(client, "dana", "acme/app", "src/billing/ledger")
    _claim(client, "kofi", "Acme/App", "src/search/index")
    everything = _feed(client)

    for spelling in ("Acme/App", "acme/app", "ACME/APP"):
        filtered = _feed(client, repo=spelling)
        # Either the parameter is not a filter (today), or it must match every spelling of the repo.
        assert len(filtered) in (len(everything), 2), (
            f"?repo={spelling} returned {[e['repo'] for e in filtered]}: a filter that matches only the "
            f"spelling it was given disagrees with scope.repo_key")
