"""Revoking an identity stops it, on every transport that authenticates, at once.

Onboarding was documented in detail and offboarding was not documented at all,
which at a handful of trusted people is something you do by hand and remember,
and at a hundred and thirty actors with ordinary turnover is an ex-colleague's
key still working.

The assertion that matters is not that a command edits a file. It is that the
plane refuses the revoked identity on the NEXT REQUEST — no restart, no waiting
for a cache — and that the refusal reaches every route that authenticates, since
offboarding that holds on one transport and not another is not offboarding.
"""

from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from agentco import auth
from agentco.app import create_app

SECRETS = {"dana": "dana-secret", "leaver": "leaver-secret", "operator": "op-secret"}


@pytest.fixture()
def keyfile(tmp_path):
    path = tmp_path / "keys.json"
    path.write_text(json.dumps(SECRETS))
    path.chmod(0o600)
    return path


@pytest.fixture()
def client(tmp_path, keyfile, monkeypatch):
    # Pointed at the FILE rather than given a dict, because that is the
    # deployed shape and it is the only shape in which revocation is possible
    # at all — an app handed a frozen mapping cannot be offboarded from.
    monkeypatch.setenv(auth.KEYS_ENV_VAR, str(keyfile))
    app = create_app(
        db_path=str(tmp_path / "api.sqlite3"),
        operator="operator",
        work_store=str(tmp_path / "work.jsonl"),
        sop_store=str(tmp_path / "sops.jsonl"),
    )
    return TestClient(app)


def signed(method, path, actor, body=None):
    raw = json.dumps(body).encode() if body is not None else b""
    ts = str(int(time.time()))
    return {"X-AgentCo-Actor": actor, "X-AgentCo-Timestamp": ts,
            "X-AgentCo-Signature": auth.sign(SECRETS[actor], method, path, ts, raw),
            "Content-Type": "application/json"}


def revoke(keyfile, actor):
    table = json.loads(keyfile.read_text())
    table.pop(actor, None)
    tmp = keyfile.with_suffix(".tmp")
    tmp.write_text(json.dumps(table))
    tmp.replace(keyfile)


def test_the_identity_works_until_it_is_revoked_and_then_does_not(client, keyfile):
    before = client.get("/events", headers=signed("GET", "/events", "leaver"))
    assert before.status_code == 200, before.text

    revoke(keyfile, "leaver")

    after = client.get("/events", headers=signed("GET", "/events", "leaver"))
    assert after.status_code == 401, "a revoked identity was still served"
    assert after.json()["code"] == "unauthenticated"


def test_it_takes_effect_on_the_next_request_with_no_restart(client, keyfile):
    """The property an operator has to be able to state out loud. A time-based
    cache would make the honest answer 'somewhere under the TTL'."""
    assert client.get("/events", headers=signed("GET", "/events", "leaver")).status_code == 200
    revoke(keyfile, "leaver")
    # the very next call, same process, same app object
    assert client.get("/events", headers=signed("GET", "/events", "leaver")).status_code == 401


def test_everyone_else_is_untouched(client, keyfile):
    revoke(keyfile, "leaver")
    assert client.get("/events", headers=signed("GET", "/events", "dana")).status_code == 200


@pytest.mark.parametrize("method,path,body", [
    ("GET", "/events", None),
    ("POST", "/work", {"title": "anything"}),
    ("POST", "/work/pull", {}),
    ("POST", "/scope-claims", {"repo": "acme/web", "prefixes": ["src/billing"],
                               "intent": "implement"}),
    ("POST", "/sops", {"title": "x", "task_type": "t", "purpose": "p", "trigger": "t",
                       "roles": {"r": {"kind": "agent"}}, "steps": []}),
])
def test_every_authenticated_route_refuses_the_revoked_identity(client, keyfile, method, path, body):
    """One route still serving a revoked key is the whole of the hole."""
    revoke(keyfile, "leaver")
    call = client.get if method == "GET" else client.post
    kwargs = {"headers": signed(method, path, "leaver", body)}
    if body is not None:
        kwargs["content"] = json.dumps(body)
    assert call(path, **kwargs).status_code == 401


def test_a_relayed_line_is_refused_when_the_RELAY_is_revoked(client, keyfile):
    """The outbox floor signs with the DRAINER's identity and marks the call
    `via: outbox`. So revoking the drainer stops the relay — and that is the
    half revocation can reach. Lines already written into a local outbox file by
    somebody since offboarded are a different problem, living on that person's
    machine, and this test exists to record which half is which."""
    path = "/scope-claims"
    body = {"repo": "acme/web", "prefixes": ["src/billing"], "intent": "implement"}
    headers = {**signed("POST", path, "leaver", body), "x-agentco-via": "outbox"}
    assert client.post(path, content=json.dumps(body), headers=headers).status_code == 200

    revoke(keyfile, "leaver")

    headers = {**signed("POST", path, "leaver", body), "x-agentco-via": "outbox"}
    assert client.post(path, content=json.dumps(body), headers=headers).status_code == 401


def test_an_emptied_key_table_refuses_everybody(client, keyfile):
    """Fail closed, which `load_keys` already promises: a registry that starts
    with no keys and accepts everything is the failure that ordering forecloses.
    Worth pinning here too, because "revoke the last actor" is a real operation
    and the failure would be silent and total."""
    keyfile.write_text(json.dumps({}))
    assert client.get("/events", headers=signed("GET", "/events", "dana")).status_code == 401


def test_a_deleted_key_file_refuses_everybody(client, keyfile):
    """The other shape of the same moment: an operator removes the file rather
    than emptying it. A cache keyed on the file's stamp must not keep serving
    from a table that no longer exists."""
    keyfile.unlink()
    assert client.get("/events", headers=signed("GET", "/events", "dana")).status_code == 401
