"""An agent answers to a person, and that is the unit both offboarding and the gate use.

At two or three agents per person, "actor" stops being the useful unit twice over.

Offboarding becomes a hunt: one leaver, three keys, and the one you forget still
works. And the separation check — a judged gate rests on the verifier not being
the executor — compares ACTORS, so one person's two agents can be executor and
verifier of the same work. That satisfies the letter while an agent grades its
owner's homework, and at this ratio it is the ordinary case rather than a corner.

The table keeps both shapes on purpose. A bare string is exactly what it was,
because every deployment already has one; an object adds the facts. A fact not
stated is read as unknown rather than guessed.
"""

from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from agentco import auth
from agentco.app import create_app
from agentco.work import Queue, WorkStatus

JUDGED = {"kind": "judged", "check": "somebody reads the diff",
          "max_park_seconds": 86400, "on_timeout": "escalate", "escalate_to": "role:owner"}


# --------------------------------------------------------------------------- #
# the table
# --------------------------------------------------------------------------- #


def write(tmp_path, table):
    p = tmp_path / "keys.json"
    p.write_text(json.dumps(table))
    p.chmod(0o600)
    return p


def test_a_flat_table_is_exactly_what_it_was(tmp_path):
    """Every deployment already has one. A format change that invalidates
    existing tables is a format change nobody applies."""
    path = write(tmp_path, {"alice": "s1", "bob": "s2"})
    assert auth.load_keys(path) == {"alice": "s1", "bob": "s2"}
    assert all(not i.owned for i in auth.load_identities(path).values())


def test_both_shapes_coexist_in_one_table(tmp_path):
    """Which is the migration path: add owners to the entries that have them."""
    path = write(tmp_path, {
        "alice": "s1",
        "alice-codex-01": {"secret": "s2", "owner": "alice", "label": "codex"},
    })
    assert auth.load_keys(path) == {"alice": "s1", "alice-codex-01": "s2"}
    ids = auth.load_identities(path)
    assert ids["alice-codex-01"].owner == "alice"
    assert ids["alice-codex-01"].label == "codex"
    assert ids["alice"].owner is None


def test_an_unowned_actor_is_its_own_party(tmp_path):
    """So a caller can compare two parties without branching, and a deployment
    that declares no ownership behaves exactly as it did before."""
    ids = auth.load_identities(write(tmp_path, {"alice": "s1"}))
    assert auth.owner_of("alice", ids) == "alice"
    assert auth.owner_of("nobody-in-the-table", ids) == "nobody-in-the-table"


def test_an_identity_cannot_answer_for_itself(tmp_path):
    path = write(tmp_path, {"a": {"secret": "s", "owner": "a"}})
    with pytest.raises(auth.AmbiguousIdentityError, match="its own owner"):
        auth.load_identities(path)


def test_ownership_is_one_level_and_never_a_chain(tmp_path):
    """A tool owned by a tool makes "who do I revoke" a graph walk and "is this
    the same party" a transitive question the gate would answer at request time."""
    path = write(tmp_path, {
        "person": "s",
        "tool": {"secret": "s", "owner": "person"},
        "tool-of-a-tool": {"secret": "s", "owner": "tool"},
    })
    with pytest.raises(auth.AmbiguousIdentityError, match="one level"):
        auth.load_identities(path)


def test_one_read_serves_both_views(tmp_path):
    """Two caches over one file is two answers to "is this actor still allowed",
    and revocation is exactly where those must not diverge."""
    from pathlib import Path as _P

    path = write(tmp_path, {"alice": "s1", "a-codex": {"secret": "s2", "owner": "alice"}})
    auth.load_keys(path)
    reads = {"n": 0}
    original = _P.read_text

    def counted(self, *a, **k):
        if self == path:
            reads["n"] += 1
        return original(self, *a, **k)

    _P.read_text = counted
    try:
        for _ in range(20):
            auth.load_keys(path)
            auth.load_identities(path)
        assert reads["n"] == 0
        write(tmp_path, {"alice": "s1"})          # the agent is revoked
        assert "a-codex" not in auth.load_keys(path)
        assert "a-codex" not in auth.load_identities(path)
    finally:
        _P.read_text = original


# --------------------------------------------------------------------------- #
# the gate
# --------------------------------------------------------------------------- #


def a_reported_item(queue, executor):
    item = queue.create("work with a judged gate", verify=dict(JUDGED))
    queue.claim(item.id, executor)
    current = queue.get(item.id)
    queue.report_result(item.id, current.lease_attempt, WorkStatus.DONE, submitted_by=executor)
    return queue.get(item.id)


def test_one_persons_two_agents_cannot_execute_and_verify_the_same_work(tmp_path):
    queue = Queue(tmp_path / "work.jsonl", verifiers=["alice-claude-01", "bob-claude-01"])
    queue.owners = lambda: {"alice-codex-01": "alice", "alice-claude-01": "alice",
                            "bob-claude-01": "bob"}
    item = a_reported_item(queue, "alice-codex-01")

    with pytest.raises(Exception) as caught:
        queue.attest(item.id,
                     {"check": "somebody reads the diff", "exit_status": 0, "environment": "local",
                      "at": "2026-09-22T10:00:00Z", "submitted_by": "alice-claude-01"},
                     "alice-claude-01", capabilities=["verify"])
    assert "answer to 'alice'" in str(caught.value)


def test_somebody_elses_agent_can_verify_it(tmp_path):
    """The rule has to still permit the thing it is for. A check that refused
    everyone would pass the test above and make the gate unanswerable."""
    queue = Queue(tmp_path / "work.jsonl", verifiers=["bob-claude-01"])
    queue.owners = lambda: {"alice-codex-01": "alice", "bob-claude-01": "bob"}
    item = a_reported_item(queue, "alice-codex-01")

    queue.attest(item.id,
                 {"check": "somebody reads the diff", "exit_status": 0, "environment": "local",
                  "at": "2026-09-22T10:00:00Z", "submitted_by": "bob-claude-01"},
                 "bob-claude-01", capabilities=["verify"])
    assert queue.get(item.id).status is WorkStatus.DONE


def test_with_no_ownership_declared_the_old_rule_is_unchanged(tmp_path):
    """A deployment that adopts none of this must behave exactly as before:
    a different actor verifies, the same actor does not."""
    queue = Queue(tmp_path / "work.jsonl", verifiers=["carol"])
    item = a_reported_item(queue, "dave")
    queue.attest(item.id,
                 {"check": "somebody reads the diff", "exit_status": 0, "environment": "local",
                  "at": "2026-09-22T10:00:00Z", "submitted_by": "carol"},
                 "carol", capabilities=["verify"])
    assert queue.get(item.id).status is WorkStatus.DONE


def test_a_table_that_cannot_be_read_falls_back_to_the_stricter_rule(tmp_path):
    """An unreadable key table must not turn every attestation into a 500, and
    must not become a way to bypass the check either. Falling back to comparing
    actors is the pre-ownership rule, which is stricter than nothing."""
    queue = Queue(tmp_path / "work.jsonl", verifiers=["alice-claude-01"])

    def broken():
        raise RuntimeError("the table is gone")

    queue.owners = broken
    item = a_reported_item(queue, "alice-codex-01")
    queue.attest(item.id,
                 {"check": "somebody reads the diff", "exit_status": 0, "environment": "local",
                  "at": "2026-09-22T10:00:00Z", "submitted_by": "alice-claude-01"},
                 "alice-claude-01", capabilities=["verify"])
    assert queue.get(item.id).status is WorkStatus.DONE


# --------------------------------------------------------------------------- #
# offboarding a person, over the wire
# --------------------------------------------------------------------------- #


TABLE = {
    "operator": "op-secret",
    "alice": {"secret": "alice-secret", "label": "laptop"},
    "alice-codex-01": {"secret": "codex-secret", "owner": "alice", "label": "codex"},
    "alice-claude-01": {"secret": "claude-secret", "owner": "alice", "label": "claude-code"},
    "bob-claude-01": {"secret": "bob-secret", "owner": "bob", "label": "claude-code"},
}
SECRETS = {k: (v if isinstance(v, str) else v["secret"]) for k, v in TABLE.items()}


def signed(method, path, actor):
    ts = str(int(time.time()))
    return {"X-AgentCo-Actor": actor, "X-AgentCo-Timestamp": ts,
            "X-AgentCo-Signature": auth.sign(SECRETS[actor], method, path, ts, b"")}


def test_revoking_a_person_stops_every_tool_they_run(tmp_path, monkeypatch):
    path = write(tmp_path, TABLE)
    monkeypatch.setenv(auth.KEYS_ENV_VAR, str(path))
    client = TestClient(create_app(
        db_path=str(tmp_path / "api.sqlite3"), operator="operator",
        work_store=str(tmp_path / "work.jsonl"), sop_store=str(tmp_path / "sops.jsonl"),
    ))
    for actor in ("alice", "alice-codex-01", "alice-claude-01", "bob-claude-01"):
        assert client.get("/events", headers=signed("GET", "/events", actor)).status_code == 200

    identities = auth.load_identities(path)
    doomed = {n for n, i in identities.items() if i.owner == "alice" or n == "alice"}
    write(tmp_path, {k: v for k, v in TABLE.items() if k not in doomed})

    for actor in ("alice", "alice-codex-01", "alice-claude-01"):
        assert client.get("/events", headers=signed("GET", "/events", actor)).status_code == 401, \
            f"{actor} still works after its owner was offboarded"
    assert client.get("/events", headers=signed("GET", "/events", "bob-claude-01")).status_code == 200
