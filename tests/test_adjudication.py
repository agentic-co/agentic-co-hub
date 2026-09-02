"""Adjudication — the tag on a divergence, and who may write it.

ASOP § 3: the tag is *an adjudication, not a confession*. Whoever tags a
divergence must be a different party from the executor whose fault a `bad` tag
would admit, and the tag carries the adjudicator's identity and pointed
evidence. The claim these tests defend is that the separation is ENFORCED —
derived from what the plane recorded about who executed the item, on every
transport, never from a field a caller could set — and that a refusal writes
nothing.

`library`-style conformance through the `queue` fixture: both backends. Then
the three transports, because a rule reachable in-process only is decoration:
HTTP (its own endpoint and the `attest` rider), MCP (the rider — no thirteenth
tool), and the outbox (its own verb and the rider).
"""

from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from agentco import auth
from agentco.app import create_app
from agentco.errors import Refusal
from agentco.mcp_server import create_server
from agentco.outbox import PUSH_VERBS, Outbox, drain, registry_publisher
from agentco.publish import Registry
from agentco.work import (
    ADJUDICATION_EXISTS,
    ADJUDICATION_INVALID,
    ADJUDICATION_SELF,
    ADJUDICATION_UNEXECUTED,
    Queue,
    WorkStatus,
    executors_of,
)

JUDGED = {"kind": "judged", "check": "a reviewer reads the diff", "max_park_seconds": 900,
          "on_timeout": "escalate", "escalate_to": "release-owner"}
DETERMINISTIC = {"kind": "deterministic", "check": "pytest -q", "max_park_seconds": 900,
                 "on_timeout": "fail"}


def executed(queue, agent="kofi", **fields):
    """An item `agent` claimed and reported — the lease is released, the record stays."""
    item = queue.create("migrate the schema", **fields)
    leased = queue.claim(item.id, agent)
    queue.report_result(item.id, leased.lease_attempt, WorkStatus.DONE)
    return queue.get(item.id)


def attestation(check="a reviewer reads the diff", exit_status=0):
    return {"check": check, "exit_status": exit_status, "environment": "reviewer laptop",
            "at": "2026-09-02T15:00:00+00:00"}


# --------------------------------------------------------------------------- #
# the record
# --------------------------------------------------------------------------- #


def test_a_reviewer_tags_a_divergence_with_evidence(queue):
    item = executed(queue, metadata={"sop_ref": {"sop_id": "sop-1", "version": 3}}, by_plane=True)
    tagged = queue.adjudicate(item.id, "good", "step 3 says 'restart'; the log shows a reload sufficed",
                              adjudicator="dana")
    record = tagged.metadata["adjudication"]
    assert record["verdict"] == "good"
    assert record["by"] == "dana"
    assert record["evidence"].startswith("step 3")
    assert record["executors"] == ["kofi"], "checked against the recorded executor"
    assert record["sop_ref"] == {"sop_id": "sop-1", "version": 3}, "P4.3 routes on this"
    assert record["at"]


def test_the_verdict_is_good_or_bad_and_nothing_else(queue):
    item = executed(queue)
    with pytest.raises(Refusal) as caught:
        queue.adjudicate(item.id, "meh", "some evidence", adjudicator="dana")
    assert caught.value.code == ADJUDICATION_INVALID
    assert "good" in caught.value.remediation and "bad" in caught.value.remediation


def test_evidence_is_required(queue):
    item = executed(queue)
    for evidence in (None, "", "   ", 42):
        with pytest.raises(Refusal) as caught:
            queue.adjudicate(item.id, "bad", evidence, adjudicator="dana")
        assert caught.value.code == ADJUDICATION_INVALID
    assert "adjudication" not in (queue.get(item.id).metadata or {})


# --------------------------------------------------------------------------- #
# adjudicator ≠ executor — enforced
# --------------------------------------------------------------------------- #


def test_the_executor_cannot_adjudicate_its_own_work(queue):
    """The lease is released on report, so `leased_by` has forgotten kofi.
    The record has not."""
    item = executed(queue, agent="kofi")
    assert item.leased_by is None
    with pytest.raises(Refusal) as caught:
        queue.adjudicate(item.id, "good", "I judged my shortcut acceptable", adjudicator="kofi")
    assert caught.value.code == ADJUDICATION_SELF
    assert caught.value.http_status == 403
    assert "adjudication" not in (queue.get(item.id).metadata or {})


def test_the_current_lease_holder_cannot_adjudicate_either(queue):
    item = queue.create("in flight")
    queue.claim(item.id, "kofi")
    with pytest.raises(Refusal) as caught:
        queue.adjudicate(item.id, "bad", "x", adjudicator="kofi")
    assert caught.value.code == ADJUDICATION_SELF


def test_a_deterministic_attester_is_an_executor_too(queue):
    """On a deterministic gate the party that attests IS the executor by
    design. A re-run by another node still puts that node in the executor set."""
    item = queue.create("gated", verify=DETERMINISTIC)
    leased = queue.claim(item.id, "kofi")
    queue.report_result(item.id, leased.lease_attempt, WorkStatus.DONE,
                        attestation=attestation("pytest -q", exit_status=1))
    assert queue.get(item.id).status == WorkStatus.VERIFY_FAILED
    queue.attest(item.id, attestation("pytest -q", exit_status=0), submitted_by="ci-box")
    assert set(executors_of(queue.get(item.id))) == {"kofi", "ci-box"}
    for who in ("kofi", "ci-box"):
        with pytest.raises(Refusal) as caught:
            queue.adjudicate(item.id, "good", "x", adjudicator=who)
        assert caught.value.code == ADJUDICATION_SELF
    assert queue.adjudicate(item.id, "good", "the flake was the check, not the work", adjudicator="dana")


def test_an_unexecuted_item_has_no_divergence_to_judge(queue):
    item = queue.create("never claimed")
    with pytest.raises(Refusal) as caught:
        queue.adjudicate(item.id, "bad", "x", adjudicator="dana")
    assert caught.value.code == ADJUDICATION_UNEXECUTED


def test_an_empty_adjudicator_is_a_judgement_from_nobody(queue):
    item = executed(queue)
    for nobody in ("", "  ", None):
        with pytest.raises(Refusal) as caught:
            queue.adjudicate(item.id, "good", "x", adjudicator=nobody)
        assert caught.value.code == ADJUDICATION_INVALID


def test_an_adjudication_is_immutable(queue):
    item = executed(queue)
    queue.adjudicate(item.id, "good", "x", adjudicator="dana")
    with pytest.raises(Refusal) as caught:
        queue.adjudicate(item.id, "bad", "y", adjudicator="eve")
    assert caught.value.code == ADJUDICATION_EXISTS
    assert caught.value.http_status == 409
    assert queue.get(item.id).metadata["adjudication"]["verdict"] == "good"


def test_the_key_cannot_be_forged_at_create(queue):
    with pytest.raises(Refusal) as caught:
        queue.create("pre-judged", metadata={"adjudication": {"verdict": "good", "by": "kofi"}})
    assert caught.value.code == "metadata_reserved"


def test_a_refused_adjudication_writes_nothing(tmp_path):
    queue = Queue(tmp_path / "work.jsonl")
    item = executed(queue, agent="kofi")
    before = (tmp_path / "work.jsonl").read_bytes()
    with pytest.raises(Refusal):
        queue.adjudicate(item.id, "good", "x", adjudicator="kofi")
    assert (tmp_path / "work.jsonl").read_bytes() == before


def test_an_unknown_item_is_none_not_an_error(queue):
    assert queue.adjudicate("w-deadbeef", "good", "x", adjudicator="dana") is None


# --------------------------------------------------------------------------- #
# the rider on attest
# --------------------------------------------------------------------------- #


def test_a_verifier_tags_the_divergence_in_the_same_call_as_the_verdict(queue):
    item = queue.create("judged", verify=JUDGED)
    leased = queue.claim(item.id, "kofi")
    queue.report_result(item.id, leased.lease_attempt, WorkStatus.DONE)
    assert queue.get(item.id).status == WorkStatus.AWAITING_VERIFY

    closed = queue.attest(item.id, attestation(), submitted_by="dana", capabilities=["verify"],
                          adjudication={"verdict": "bad", "evidence": "skipped the reproduce step"})
    assert closed.status == WorkStatus.DONE
    assert closed.metadata["adjudication"]["verdict"] == "bad"
    assert closed.metadata["adjudication"]["by"] == "dana"


def test_an_executor_offering_a_rider_is_refused_whole(queue):
    """Nothing lands — not the attestation either. The remediation says to
    attest without it, so a valid verdict is one retry away, not lost."""
    item = queue.create("gated", verify=DETERMINISTIC)
    leased = queue.claim(item.id, "kofi")
    queue.report_result(item.id, leased.lease_attempt, WorkStatus.DONE,
                        attestation=attestation("pytest -q", exit_status=1))
    with pytest.raises(Refusal) as caught:
        queue.attest(item.id, attestation("pytest -q", 0), submitted_by="kofi",
                     adjudication={"verdict": "good", "evidence": "trust me"})
    assert caught.value.code == ADJUDICATION_SELF
    after = queue.get(item.id)
    assert after.status == WorkStatus.VERIFY_FAILED, "the attestation did not land either"
    assert "adjudication" not in (after.metadata or {})


def test_a_malformed_rider_is_refused_before_the_verdict(queue):
    item = queue.create("judged", verify=JUDGED)
    leased = queue.claim(item.id, "kofi")
    queue.report_result(item.id, leased.lease_attempt, WorkStatus.DONE)
    with pytest.raises(Refusal) as caught:
        queue.attest(item.id, attestation(), submitted_by="dana", capabilities=["verify"],
                     adjudication="good")
    assert caught.value.code == ADJUDICATION_INVALID
    assert queue.get(item.id).status == WorkStatus.AWAITING_VERIFY


# --------------------------------------------------------------------------- #
# HTTP — the adjudicator is the signature
# --------------------------------------------------------------------------- #

KEYS = {"kofi": "kofi-secret", "dana": "dana-secret", "operator": "op-secret",
        "bigmac": "bigmac-secret", "reviewer-box": "reviewer-secret"}


def _client(tmp_path):
    return TestClient(create_app(
        db_path=str(tmp_path / "api.sqlite3"), keys=KEYS, operator="operator",
        work_store=str(tmp_path / "work.jsonl"), sop_store=str(tmp_path / "sops.jsonl"),
    ))


def _post(client, path, actor, body):
    raw = json.dumps(body).encode()
    ts = str(int(time.time()))
    return client.post(path, content=raw, headers={
        "X-AgentCo-Actor": actor, "X-AgentCo-Timestamp": ts,
        "X-AgentCo-Signature": auth.sign(KEYS[actor], "POST", path, ts, raw),
        "Content-Type": "application/json",
    })


def _executed_over_http(client, actor="kofi", **fields):
    item = _post(client, "/work", "operator", {"title": "migrate", **fields}).json()["item"]
    pulled = _post(client, "/work/pull", actor, {}).json()
    assert pulled["state"] == "leased"
    reported = _post(client, f"/work/{item['id']}/report", actor,
                     {"attempt": pulled["attempt"], "status": "done"})
    assert reported.status_code == 200, reported.text
    return item


def test_over_http_the_executor_is_refused_and_a_reviewer_is_not(tmp_path):
    client = _client(tmp_path)
    item = _executed_over_http(client)

    own = _post(client, f"/work/{item['id']}/adjudicate", "kofi",
                {"verdict": "good", "evidence": "my own shortcut was fine"})
    assert own.status_code == 403, own.text
    assert own.json()["code"] == ADJUDICATION_SELF

    theirs = _post(client, f"/work/{item['id']}/adjudicate", "dana",
                   {"verdict": "bad", "evidence": "the reproduce step was skipped, see the log"})
    assert theirs.status_code == 200, theirs.text
    assert theirs.json()["item"]["metadata"]["adjudication"]["by"] == "dana"


def test_over_http_the_body_cannot_name_the_adjudicator(tmp_path):
    """Refused, not ignored: the executor writing `adjudicator: dana` must not
    believe dana's tag was recorded."""
    client = _client(tmp_path)
    item = _executed_over_http(client)
    for key in ("adjudicator", "by", "actor"):
        response = _post(client, f"/work/{item['id']}/adjudicate", "kofi",
                         {"verdict": "good", "evidence": "x", key: "dana"})
        assert response.status_code == 400, (key, response.text)
    # Nothing was recorded under dana's name by any of those.
    stored = Queue(tmp_path / "work.jsonl").get(item["id"])
    assert "adjudication" not in (stored.metadata or {})


def test_over_http_the_rider_on_attest_is_written_with_the_verdict(tmp_path):
    client = _client(tmp_path)
    item = _executed_over_http(client, verify=JUDGED)
    answered = _post(client, f"/work/{item['id']}/attest", "dana", {
        "attestation": attestation(), "capabilities": ["verify"],
        "adjudication": {"verdict": "good", "evidence": "step 2 is redundant; the diff shows why"},
    })
    assert answered.status_code == 200, answered.text
    body = answered.json()["item"]
    assert body["status"] == "done"
    assert body["metadata"]["adjudication"]["verdict"] == "good"


def test_over_http_an_unknown_item_is_a_404_with_a_code(tmp_path):
    client = _client(tmp_path)
    response = _post(client, "/work/w-deadbeef/adjudicate", "dana", {"verdict": "good", "evidence": "x"})
    assert response.status_code == 404
    assert response.json()["code"] == "work_item_unknown"


# --------------------------------------------------------------------------- #
# MCP — the rider, no thirteenth tool
# --------------------------------------------------------------------------- #


def test_over_mcp_the_rider_is_on_attest_and_the_process_identity_is_the_adjudicator(tmp_path):
    executor = create_server(
        db_path=str(tmp_path / "r.sqlite3"), work_store=str(tmp_path / "work.jsonl"),
        sop_store=str(tmp_path / "sops.jsonl"), actor="kofi",
    )
    reviewer = create_server(
        db_path=str(tmp_path / "r.sqlite3"), work_store=str(tmp_path / "work.jsonl"),
        sop_store=str(tmp_path / "sops.jsonl"), actor="dana",
    )
    tool = lambda server, name: server._tool_manager.get_tool(name).fn  # noqa: E731
    item = tool(executor, "work_create")(title="judged", verify=JUDGED)
    pulled = tool(executor, "work_pull")()
    tool(executor, "work_report")(item_id=item["id"], attempt=pulled["lease_attempt"], status="done")

    names = sorted(t.name for t in reviewer._tool_manager.list_tools())
    assert "adjudicate" not in names, "no thirteenth tool — the rider is the MCP path"

    with pytest.raises(Exception) as caught:
        tool(executor, "attest")(item_id=item["id"], attestation=attestation(),
                                 adjudication={"verdict": "good", "evidence": "x"})
    assert "executed" in str(caught.value) or ADJUDICATION_SELF in str(caught.value)

    import os
    os.environ["AGENTCO_CAPABILITIES"] = "verify"
    try:
        closed = tool(reviewer, "attest")(item_id=item["id"], attestation=attestation(),
                                          adjudication={"verdict": "bad", "evidence": "skipped a step"})
    finally:
        del os.environ["AGENTCO_CAPABILITIES"]
    assert closed["metadata"]["adjudication"] == {
        **closed["metadata"]["adjudication"], "by": "dana", "verdict": "bad"
    }


# --------------------------------------------------------------------------- #
# outbox — the verb, and the machine credential as adjudicator
# --------------------------------------------------------------------------- #


class LoopbackRegistry(Registry):
    def __init__(self, actor: str, client: TestClient):
        super().__init__(actor, KEYS[actor], "http://registry.test", via="outbox")
        self.client = client

    def _call(self, method, path, body=None, query=""):
        raw = json.dumps(body).encode() if body is not None else b""
        ts = str(int(time.time()))
        response = self.client.request(method, f"{path}{query}", content=raw or None, headers={
            "X-AgentCo-Actor": self.actor, "X-AgentCo-Timestamp": ts,
            "X-AgentCo-Signature": auth.sign(self.secret, method, path, ts, raw),
            "Content-Type": "application/json", "X-AgentCo-Via": "outbox",
        })
        if response.status_code >= 400:
            from agentco.publish import RegistryError
            raise RegistryError(response.status_code, response.json())
        return response.json()


def test_adjudicate_is_in_the_push_set():
    assert "adjudicate" in PUSH_VERBS


def test_over_the_outbox_the_executing_machine_is_refused_and_another_is_not(tmp_path):
    client = _client(tmp_path)
    item = _executed_over_http(client, actor="bigmac")

    same_machine = Outbox(tmp_path / "bigmac" / ".agentco")
    same_machine.push("adjudicate", {"itemId": item["id"], "verdict": "good", "evidence": "x"},
                      agent_label="aider")
    result = drain(same_machine, registry_publisher(LoopbackRegistry("bigmac", client)))
    assert result["published"] == 0, result
    assert result["refused"] == 1, result
    assert "adjudication" not in (Queue(tmp_path / "work.jsonl").get(item["id"]).metadata or {})

    elsewhere = Outbox(tmp_path / "reviewer" / ".agentco")
    elsewhere.push("adjudicate", {"itemId": item["id"], "verdict": "bad",
                                  "evidence": "the transcript shows the check was never run"},
                   agent_label="aider")
    result = drain(elsewhere, registry_publisher(LoopbackRegistry("reviewer-box", client)))
    assert result["published"] == 1, result
    rows = list(client.app.state.conn.execute(
        "SELECT verb, via, agent_label FROM calls WHERE verb='adjudicate' ORDER BY at"
    ))
    assert [(r["verb"], r["via"], r["agent_label"]) for r in rows][-1] == ("adjudicate", "outbox", "aider")


def test_over_the_outbox_an_attest_line_carries_the_rider(tmp_path):
    client = _client(tmp_path)
    item = _executed_over_http(client, actor="bigmac", verify=JUDGED)
    box = Outbox(tmp_path / "reviewer" / ".agentco")
    box.push("attest", {
        "itemId": item["id"], "capabilities": ["verify"], "attestation": attestation(),
        "adjudication": {"verdict": "good", "evidence": "the procedure's step 4 is wrong, diff attached"},
    })
    result = drain(box, registry_publisher(LoopbackRegistry("reviewer-box", client)))
    assert result["published"] == 1, result
    stored = Queue(tmp_path / "work.jsonl").get(item["id"])
    assert stored.status == WorkStatus.DONE
    assert stored.metadata["adjudication"]["by"] == "reviewer-box", "the machine credential adjudicated"


# --------------------------------------------------------------------------- #
# second-party findings (Max, c34530c) — each closed with the test that caught it
# --------------------------------------------------------------------------- #

from datetime import datetime, timedelta, timezone  # noqa: E402


def test_a_rider_may_not_name_its_adjudicator(queue):
    """Finding 2: a rider carrying `by` was ignored; two mutants that honoured
    it survived. Now it is refused, and nothing lands."""
    item = queue.create("judged", verify=JUDGED)
    leased = queue.claim(item.id, "kofi")
    queue.report_result(item.id, leased.lease_attempt, WorkStatus.DONE)
    with pytest.raises(Refusal) as caught:
        queue.attest(item.id, attestation(), submitted_by="kofi", capabilities=["verify"],
                     adjudication={"verdict": "good", "evidence": "x", "by": "dana"})
    assert caught.value.code == ADJUDICATION_INVALID and "['by']" in caught.value.message
    after = queue.get(item.id)
    assert after.status == WorkStatus.AWAITING_VERIFY and "adjudication" not in (after.metadata or {})


def test_a_deterministic_gate_rider_is_refused_before_the_verdict_lands(queue):
    """Finding 3: a non-executor's rider on a deterministic gate landed the
    attestation and THEN refused the tag — a partial write. Now the whole call
    is refused up front: attesting a deterministic gate makes you an executor."""
    item = queue.create("gated", verify=DETERMINISTIC)
    leased = queue.claim(item.id, "kofi")
    queue.report_result(item.id, leased.lease_attempt, WorkStatus.DONE,
                        attestation=attestation("pytest -q", exit_status=1))
    with pytest.raises(Refusal) as caught:
        queue.attest(item.id, attestation("pytest -q", 0), submitted_by="dana",
                     adjudication={"verdict": "good", "evidence": "the check was flaky"})
    assert caught.value.code == ADJUDICATION_SELF
    after = queue.get(item.id)
    assert after.status == WorkStatus.VERIFY_FAILED, "the attestation did not land"
    assert (after.attestation or {}).get("submitted_by") != "dana"
    # Without the rider the same attestation is fine, and dana adjudicates separately? No —
    # dana is now an executor; a third party adjudicates.
    queue.attest(item.id, attestation("pytest -q", 0), submitted_by="dana")
    with pytest.raises(Refusal):
        queue.adjudicate(item.id, "good", "x", adjudicator="dana")
    assert queue.adjudicate(item.id, "good", "the check was flaky", adjudicator="eve")


def test_a_reaped_first_holder_is_still_an_executor(queue):
    """Finding 4: kofi's lease lapsed and was reaped; dana finished the work.
    For idempotent work dana reported what kofi did — kofi may not grade it."""
    t0 = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)
    item = queue.create("idempotent export")
    assert queue.claim(item.id, "kofi", ttl_seconds=60, now=t0) is not None
    reaped = queue.reap_expired_leases(now=t0 + timedelta(minutes=5))
    assert [r.id for r in reaped] == [item.id]
    leased = queue.claim(item.id, "dana", now=t0 + timedelta(minutes=6))
    queue.report_result(item.id, leased.lease_attempt, WorkStatus.DONE)
    assert set(executors_of(queue.get(item.id))) == {"kofi", "dana"}
    for who in ("kofi", "dana"):
        with pytest.raises(Refusal) as caught:
            queue.adjudicate(item.id, "good", "x", adjudicator=who)
        assert caught.value.code == ADJUDICATION_SELF
    assert queue.adjudicate(item.id, "good", "x", adjudicator="eve")


def test_annotate_cannot_forge_or_erase_plane_owned_keys(queue):
    """Finding 1: `annotate` merged anything, including `adjudication` (a
    forged tag) and `lease_report` (erasing the executor). Reserved keys now
    need `by_plane`, which no transport can pass."""
    item = executed(queue, agent="kofi")
    for key in ("adjudication", "lease_report", "claims", "verifies", "plan_vs_actual"):
        with pytest.raises(Refusal) as caught:
            queue.annotate(item.id, {key: {"forged": True}})
        assert caught.value.code == "metadata_reserved"
    assert queue.get(item.id).metadata["lease_report"]["reported_by"] == "kofi"
    with pytest.raises(Refusal):
        queue.adjudicate(item.id, "good", "x", adjudicator="kofi")
    # The plane's own passes still write their keys.
    tagged = queue.annotate(item.id, {"verify_escalated": {"to": "dana"}}, by_plane=True)
    assert tagged.metadata["verify_escalated"] == {"to": "dana"}
    assert queue.annotate(item.id, {"note": "ordinary keys are fine"}).metadata["note"]


def test_the_pin_cannot_be_forged_at_create(queue):
    """Second party on P4.V: with `sop_ref` settable, any agent+reviewer pair
    could pin an item to a procedure it never ran, adjudicate it bad, and feed
    that procedure's lesson channel through the pass."""
    with pytest.raises(Refusal) as caught:
        queue.create("never ran it", metadata={"sop_ref": {"sop_id": "sop-victim", "version": 1}})
    assert caught.value.code == "metadata_reserved"
    assert queue.list() == []
