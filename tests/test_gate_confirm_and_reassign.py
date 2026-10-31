"""The grace confirmation (ADR 0008 §8 S4) and gate reassignment (Decision 4).

What would fail if either were dishonest: the hub handing a partition the
verdict of a held answer (so "confirm" would confirm the hub's claim, not the
person's); a held answer lingering after a fresh one answered its slot; one
person's held answers listed to another; a removed person's open gate left
naming them, or their vehicle still asking them; a done gate rewritten; a
removed person's signed answer still landing after the move; a non-admin
moving gates. Synthetic actors and keys only.
"""

from __future__ import annotations

import json
import time
from datetime import timedelta

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519
from fastapi.testclient import TestClient

from agentco import auth, envelope, humankeys, ownership, verifiers
from agentco.app import create_app
from agentco.errors import Refusal
from agentco.work import WorkStatus
from tests.test_envelope import HUMAN, NOW, payload_for, row, send, world  # noqa: F401
from tests.test_humankeys import fingerprint, pem

pytestmark = pytest.mark.skipif(not envelope.SUPPORTED,
                                reason="installed asop-spec predates canonical JSON (0.6.0)")


def late(conn, item_id, private, **over):
    return payload_for(conn, item_id, private, issued_at=int((NOW - timedelta(hours=30)).timestamp()), **over)


# --------------------------------------------------------------------------- #
# S4: held answers and their confirmation
# --------------------------------------------------------------------------- #


def test_held_answers_are_listed_by_slot_and_digest_never_by_verdict(world):
    conn, queue, item_id, private = world
    held = late(conn, item_id, private)
    send(conn, queue, held, private)
    [listed] = envelope.held_for(conn, "dana")
    assert (listed["item"], listed["verifyAttempt"], listed["nonce"]) == (item_id, 0, held["nonce"])
    assert listed["digest"] == envelope.digest(held), "the partition matches its own record by digest"
    assert "verdict" not in json.dumps(listed) and "exercised" not in json.dumps(listed)
    assert envelope.held_for(conn, "eli") == []


def test_a_fresh_answer_confirms_and_the_held_one_expires_untouched(world):
    conn, queue, item_id, private = world
    held = late(conn, item_id, private, verdict={"passed": False, "reason": "typed on the plane"})
    send(conn, queue, held, private)
    confirm = payload_for(conn, item_id, private)  # a NEW envelope, fresh, new nonce
    assert send(conn, queue, confirm, private)["state"] == "applied"
    assert queue.get(item_id).status is WorkStatus.DONE
    assert queue.get(item_id).verify_failures == 0, "the held verdict never touched the item"
    stored = row(conn, held)
    assert stored["status"] == "expired"
    assert stored["receipt"] == {"supersededBy": {"keyId": confirm["key_id"], "nonce": confirm["nonce"]}}
    assert envelope.held_for(conn, "dana") == []
    assert send(conn, queue, held, private)["state"] == "expired", "a resend of it stays expired"


def test_an_answer_for_another_slot_leaves_a_held_one_alone(world):
    conn, queue, item_id, private = world
    other = queue.create("another gate", verify=HUMAN)
    claimed = queue.claim(other.id, "hub-worker")
    queue.report_result(other.id, claimed.lease_attempt, WorkStatus.DONE)
    held = late(conn, item_id, private)
    send(conn, queue, held, private)
    send(conn, queue, payload_for(conn, other.id, private), private)
    assert row(conn, held)["status"] == "held"


# --------------------------------------------------------------------------- #
# Decision 4: reassigning a removed person's gates
# --------------------------------------------------------------------------- #


def parked(queue, title, gate=HUMAN):
    item = queue.create(title, verify=gate)
    claimed = queue.claim(item.id, "hub-worker")
    queue.report_result(item.id, claimed.lease_attempt, WorkStatus.DONE)
    return item.id


def test_every_open_gate_naming_the_person_moves_with_its_vehicle(queue):
    queue.declare(humans=["dana", "eli", "fay"])
    waiting = parked(queue, "waiting on dana")
    in_progress = queue.create("still running", verify=HUMAN).id
    done = parked(queue, "answered already")
    queue.attest(done, {"check": HUMAN["check"], "exit_status": 0, "environment": "laptop",
                        "at": "2026-10-30T00:00:00+00:00", "verdict": {"passed": True, "reason": "ok"}}, "dana")
    fays = parked(queue, "fay's", gate={**HUMAN, "verifier": "fay", "escalate_to": "fay"})
    verifiers.route_open_gates(queue)
    [vehicle] = [v for v in queue.list() if verifiers.is_vehicle(v) and verifiers.verifies(v) == waiting]
    assert vehicle.assigned_agent == "dana"

    result = ownership.reassign_gates(queue, from_="dana", to="eli", reason="left the company",
                                      by="admin", at="t1", humans={"dana", "eli", "fay"})
    assert sorted(result["reassigned"]) == sorted([waiting, in_progress])
    for item_id in (waiting, in_progress):
        gate = queue.get(item_id).verify
        assert (gate["verifier"], gate["escalate_to"]) == ("eli", "eli")
        [record] = queue.get(item_id).metadata[ownership.GATE_REASSIGNED_KEY]
        assert record == {"at": "t1", "by": "admin", "from": "dana", "to": "eli", "reason": "left the company"}
    assert queue.get(done).verify["verifier"] == "dana", "an answered gate keeps who answered it"
    assert queue.get(fays).verify["verifier"] == "fay"
    assert result["vehicles"] == [vehicle.id] and queue.get(vehicle.id).assigned_agent == "eli"
    assert queue.get(vehicle.id).metadata["gate"]["verifier"] == "eli"


@pytest.mark.parametrize("kw, code", [
    ({"to": "build-agent"}, "human_required"),
    ({"to": "dana"}, "reassign_invalid"),
    ({"reason": " "}, "reassign_invalid"),
    ({"items": ["w-nope"]}, "reassign_invalid"),
])
def test_a_bad_reassignment_moves_nothing(queue, kw, code):
    queue.declare(humans=["dana", "eli"])
    item_id = parked(queue, "waiting on dana")
    args = {"from_": "dana", "to": "eli", "reason": "left", "by": "admin", "at": "t",
            "humans": {"dana", "eli"}, **kw}
    with pytest.raises(Refusal) as refused:
        ownership.reassign_gates(queue, **args)
    assert refused.value.code == code
    assert queue.get(item_id).verify["verifier"] == "dana"


def test_after_the_move_the_removed_persons_signed_answer_is_refused(world):
    conn, queue, item_id, private = world
    eli = ed25519.Ed25519PrivateKey.generate()
    humankeys.enrol(conn, human="eli", public_key=pem(eli), fingerprint=fingerprint(eli),
                    by="op", humans={"dana", "eli"}, label="acme-hub")
    ownership.reassign_gates(queue, from_="dana", to="eli", reason="left", by="admin", at="t",
                             humans={"dana", "eli"})
    with pytest.raises(Refusal) as refused:
        send(conn, queue, payload_for(conn, item_id, private), private)
    assert refused.value.code == "attestation_invalid" and "names 'eli'" in refused.value.message
    answer = payload_for(conn, item_id, eli, signer="eli")
    assert send(conn, queue, answer, eli, transport_party="eli", via="relay-eli")["state"] == "applied"


# --------------------------------------------------------------------------- #
# over HTTP
# --------------------------------------------------------------------------- #

KEYS = {"admin": "admin-secret", "dana": "dana-secret", "eli": "eli-secret", "hub-worker": "hw-secret",
        "relay-dana": "rd-secret"}


def http(client, method, path, actor, body=None):
    raw = json.dumps(body).encode() if body is not None else b""
    ts = str(int(time.time()))
    headers = {"X-AgentCo-Actor": actor, "X-AgentCo-Timestamp": ts,
               "X-AgentCo-Signature": auth.sign(KEYS[actor], method, path, ts, raw),
               "Content-Type": "application/json"}
    return client.request(method, path, content=raw or None, headers=headers)


def test_http_held_list_and_admin_only_reassignment(tmp_path):
    app = create_app(db_path=str(tmp_path / "api.sqlite3"), keys=KEYS, operator="admin", hub_id="acme-hub",
                     work_store=str(tmp_path / "work.jsonl"), sop_store=str(tmp_path / "sops.jsonl"),
                     humans=["admin", "dana", "eli"], admins=["admin"], transports={"relay-dana": "dana"})
    client = TestClient(app)
    conn, queue = app.state.conn, app.state.queue
    private = ed25519.Ed25519PrivateKey.generate()
    humankeys.enrol(conn, human="dana", public_key=pem(private), fingerprint=fingerprint(private),
                    by="op", humans={"dana"}, label="acme-hub")
    item_id = parked(queue, "waiting on dana")

    held = payload_for(conn, item_id, private, issued_at=int(time.time()) - 30 * 3600)
    assert http(client, "POST", "/answers", "relay-dana",
                {"envelope": json.dumps(held), "signature": envelope.sign(private, held)}).status_code == 202
    listed = http(client, "GET", "/answers/held", "relay-dana").json()
    assert listed["count"] == 1 and listed["held"][0]["nonce"] == held["nonce"]
    assert http(client, "GET", "/answers/held", "eli").json()["count"] == 0

    body = {"from": "dana", "to": "eli", "reason": "left the company"}
    assert http(client, "POST", "/gates/reassign", "dana", body).json()["code"] == "transfer_refused:administrator"
    assert http(client, "POST", "/gates/reassign", "hub-worker", body).json()["code"] == "human_required"
    moved = http(client, "POST", "/gates/reassign", "admin", body)
    assert moved.status_code == 200 and moved.json()["reassigned"] == [item_id]
    feed = conn.execute("SELECT payload FROM events WHERE kind = 'GateReassigned'").fetchall()
    assert json.loads(feed[0]["payload"])["items"] == [item_id]
