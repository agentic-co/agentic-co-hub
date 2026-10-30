"""Signed answers to human gates (ADR 0008 §3.3, §8 B1/B2/S3/S5; agentco/envelope.py).

What would fail if the answer path were dishonest: an unsigned answer landing
in the name of a person who has a key; an answer that verifies under nobody's
key, under a revoked key, or for another hub; a flipped verdict keeping its
signature; one envelope applied twice, or a crash between the nonce row and the
attestation leaving the answer lost or doubled; two answers racing for one slot
both landing; a late answer landing on a click; a relay carrying someone else's
answer, or reading someone else's receipt. Synthetic actors and keys only.
"""

from __future__ import annotations

import base64
import json
import secrets
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric import ec, ed25519
from fastapi.testclient import TestClient

from agentco import auth, db, envelope, humankeys, hubid
from agentco.app import create_app
from agentco.errors import Refusal
from agentco.work import Queue, WorkStatus
from tests.test_humankeys import fingerprint, pem

pytestmark = pytest.mark.skipif(not envelope.SUPPORTED,
                                reason="installed asop-spec predates canonical JSON (0.6.0)")

HUMAN = {"kind": "human", "check": "a person confirms the rollback was exercised", "verifier": "dana",
         "max_park_seconds": 3600, "on_timeout": "escalate", "escalate_to": "dana"}
NOW = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
VECTORS = Path(__file__).resolve().parents[1] / "docs" / "envelope-vectors.json"


@pytest.fixture()
def world(tmp_path):
    """A registry with dana's Ed25519 key enrolled, and a queue holding one item
    parked on dana's human gate. Returns (conn, queue, item_id, private_key)."""
    conn = db.connect(tmp_path / "r.sqlite3")
    hubid.ensure(conn)
    queue = Queue(tmp_path / "work.jsonl", humans=["dana", "eli"])
    queue.envelope_required_for = envelope.required_for(conn)
    private = ed25519.Ed25519PrivateKey.generate()
    humankeys.enrol(conn, human="dana", public_key=pem(private), fingerprint=fingerprint(private),
                    by="op", humans={"dana", "eli"}, label="acme-hub")
    item = queue.create("ship the rollback", verify=HUMAN)
    claimed = queue.claim(item.id, "hub-worker")
    queue.report_result(item.id, claimed.lease_attempt, WorkStatus.DONE)
    assert queue.get(item.id).status is WorkStatus.AWAITING_VERIFY
    return conn, queue, item.id, private


def payload_for(conn, item_id, private, **overrides):
    key_id = humankeys.key_id_of(fingerprint(private))
    body = {"type": envelope.TYPE, "hub": hubid.read(conn), "item": item_id, "verify_attempt": 0,
            "stage": None, "epoch": 0, "verdict": {"passed": True, "reason": "exercised on staging"},
            "choice": None, "signer": "dana", "key_id": key_id, "nonce": secrets.token_hex(16),
            "issued_at": int(NOW.timestamp())}
    body.update(overrides)
    return body


def send(conn, queue, payload, private=None, *, signature=None, text=None, at=NOW, **kw):
    sig = signature if signature is not None else envelope.sign(private, payload)
    return envelope.submit(conn, queue, envelope=text if text is not None else json.dumps(payload),
                           signature=sig, via=kw.pop("via", "relay-dana"),
                           hub_uid=hubid.read(conn), now=at, **kw)


def row(conn, payload):
    return envelope.receipt(conn, payload["key_id"], payload["nonce"])


# --------------------------------------------------------------------------- #
# the contract: vectors, bytes, signatures
# --------------------------------------------------------------------------- #


def test_the_published_vectors_are_what_this_code_signs_and_accepts():
    import importlib.util

    spec = importlib.util.spec_from_file_location("vectors", VECTORS.parents[1] / "tools" / "envelope_vectors.py")
    gen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gen)
    published = json.loads(VECTORS.read_text())
    ed, p256 = gen.test_keys()
    for vector, private in zip(published["vectors"], (ed, p256)):
        payload = vector["payload"]
        assert envelope.signed_bytes(payload).decode() == vector["signedBytes"]
        assert vector["signedBytes"].startswith("asop.answer/1\n{"), "type line, LF, canonical JSON"
        key = humankeys.HumanKey(key_id=payload["key_id"], human="dana", alg=vector["alg"],
                                 public_key=vector["publicKeySpki"], fingerprint=vector["fingerprint"],
                                 hub_uid=payload["hub"], enrolled_by="op", enrolled_at="t")
        envelope.verify_signature(key, payload, vector["signature"])
        envelope.verify_signature(key, payload, envelope.sign(private, payload))
    assert published["vectors"][0]["signature"] == envelope.sign(ed, published["vectors"][0]["payload"]), \
        "Ed25519 is deterministic, so its vector pins the exact signature"


def test_a_high_s_ecdsa_signature_is_refused():
    private = ec.generate_private_key(ec.SECP256R1())
    payload = {**json.loads(VECTORS.read_text())["vectors"][1]["payload"]}
    from cryptography.hazmat.primitives import serialization

    der = private.public_key().public_bytes(serialization.Encoding.DER,
                                            serialization.PublicFormat.SubjectPublicKeyInfo)
    key = humankeys.HumanKey(key_id="k", human="dana", alg=humankeys.ECDSA_P256,
                             public_key=base64.b64encode(der).decode(), fingerprint="f",
                             hub_uid=payload["hub"], enrolled_by="op", enrolled_at="t")
    low = base64.b64decode(envelope.sign(private, payload))
    r, s = int.from_bytes(low[:32], "big"), int.from_bytes(low[32:], "big")
    high = r.to_bytes(32, "big") + (envelope._P256_N - s).to_bytes(32, "big")
    envelope.verify_signature(key, payload, base64.b64encode(low).decode())
    with pytest.raises(Refusal) as refused:
        envelope.verify_signature(key, payload, base64.b64encode(high).decode())
    assert refused.value.code == "signature_invalid", "one payload, one valid signature per key"


@pytest.mark.parametrize("mutate, fragment", [
    (lambda p: p.pop("choice"), "missing ['choice']"),
    (lambda p: p.update(extra=1), "unknown ['extra']"),
    (lambda p: p.update(verify_attempt=True), "verify_attempt"),
    (lambda p: p.update(issued_at="1793318400"), "issued_at"),
    (lambda p: p.update(nonce="ABCDEF"), "nonce"),
    (lambda p: p.update(verdict={"passed": "yes", "reason": "x"}), "verdict"),
    (lambda p: p.update(type="asop.answer/2"), "type"),
    (lambda p: p.update(stage=-1), "stage"),
])
def test_a_malformed_payload_is_refused_naming_the_field(world, mutate, fragment):
    conn, queue, item_id, private = world
    payload = payload_for(conn, item_id, private)
    mutate(payload)
    with pytest.raises(Refusal) as refused:
        send(conn, queue, payload, signature="AA==")
    assert refused.value.code == "envelope_invalid" and fragment in refused.value.message


def test_duplicate_keys_floats_and_objects_are_refused_at_parse(world):
    conn, queue, item_id, private = world
    text = json.dumps(payload_for(conn, item_id, private))
    for bad in (text[:-1] + ', "signer": "eli"}', text.replace('"epoch": 0', '"epoch": 0.0')):
        with pytest.raises(Refusal) as refused:
            send(conn, queue, {}, signature="AA==", text=bad)
        assert refused.value.code == "envelope_invalid"
    with pytest.raises(Refusal) as refused:
        envelope.submit(conn, queue, envelope=json.loads(text), signature="AA==", via="r",
                        hub_uid=hubid.read(conn), now=NOW)
    assert refused.value.code == "envelope_invalid" and "string" in refused.value.message


# --------------------------------------------------------------------------- #
# applying an answer
# --------------------------------------------------------------------------- #


def test_a_signed_answer_lands_as_the_person_and_carries_its_envelope(world):
    conn, queue, item_id, private = world
    payload = payload_for(conn, item_id, private)
    result = send(conn, queue, payload, private)
    assert result["state"] == "applied"
    item = queue.get(item_id)
    assert item.status is WorkStatus.DONE
    assert item.attestation["submitted_by"] == "dana"
    carried = item.attestation["envelope"]
    assert (carried["key_id"], carried["nonce"], carried["via"]) == (payload["key_id"], payload["nonce"], "relay-dana")
    assert carried["payload"] == payload
    assert row(conn, payload)["status"] == "applied"
    # The stored attestation survives a reload from the store.
    assert Queue(queue.path).get(item_id).attestation["envelope"]["nonce"] == payload["nonce"]


def test_a_failing_verdict_advances_the_attempt_once(world):
    conn, queue, item_id, private = world
    payload = payload_for(conn, item_id, private, verdict={"passed": False, "reason": "not yet"})
    send(conn, queue, payload, private)
    assert queue.get(item_id).verify_failures == 1
    assert queue.get(item_id).status is WorkStatus.VERIFY_FAILED


def test_b1_the_unsigned_path_closes_for_a_person_with_a_key(world):
    conn, queue, item_id, private = world
    unsigned = {"check": HUMAN["check"], "exit_status": 0, "environment": "laptop",
                "at": "2026-10-30T00:00:00+00:00", "verdict": {"passed": True, "reason": "seen"}}
    with pytest.raises(Refusal) as refused:
        queue.attest(item_id, unsigned, "dana")
    assert (refused.value.code, refused.value.http_status) == ("envelope_required", 403)
    assert queue.get(item_id).status is WorkStatus.AWAITING_VERIFY
    # Revoking every key does not reopen it: the name has had a key.
    for key in humankeys.list_keys(conn, human="dana"):
        humankeys.revoke(conn, key_id=key.key_id, by="op")
    with pytest.raises(Refusal):
        queue.attest(item_id, unsigned, "dana")
    # Someone with no key answers unsigned exactly as before, and reads as legacy.
    other = queue.create("eli's gate", verify={**HUMAN, "verifier": "eli", "escalate_to": "eli"})
    claimed = queue.claim(other.id, "hub-worker")
    queue.report_result(other.id, claimed.lease_attempt, WorkStatus.DONE)
    landed = queue.attest(other.id, unsigned, "eli")
    assert landed.status is WorkStatus.DONE and "envelope" not in landed.attestation


@pytest.mark.parametrize("case, code", [
    ("wrong_hub", "envelope_wrong_hub"),
    ("unknown_key", "key_unknown"),
    ("someone_elses_key", "key_unknown"),
    ("revoked", "key_revoked"),
    ("bad_signature", "signature_invalid"),
    ("flipped_verdict", "signature_invalid"),
    ("future", "envelope_future"),
    ("expired", "envelope_expired"),
])
def test_every_check_refuses_before_anything_changes(world, case, code):
    conn, queue, item_id, private = world
    payload, kw, sig = payload_for(conn, item_id, private), {}, None
    if case == "wrong_hub":
        payload["hub"] = "f" * 32
    elif case == "unknown_key":
        payload["key_id"] = "0" * 32
    elif case == "someone_elses_key":
        payload["signer"] = "eli"
    elif case == "revoked":
        humankeys.revoke(conn, key_id=payload["key_id"], by="op")
    elif case == "bad_signature":
        sig = envelope.sign(ed25519.Ed25519PrivateKey.generate(), payload)
    elif case == "flipped_verdict":
        sig = envelope.sign(private, payload)
        payload["verdict"] = {"passed": False, "reason": "exercised on staging"}
    elif case == "future":
        payload["issued_at"] = int(NOW.timestamp()) + envelope.MAX_FUTURE_S + 1
    elif case == "expired":
        payload["issued_at"] = int((NOW - timedelta(hours=37)).timestamp())
    with pytest.raises(Refusal) as refused:
        send(conn, queue, payload, private, signature=sig, **kw)
    assert refused.value.code == code
    assert queue.get(item_id).status is WorkStatus.AWAITING_VERIFY
    assert row(conn, payload) is None, "refused before the nonce table is touched"


def test_a_relay_carries_only_its_own_partys_answers(world):
    conn, queue, item_id, private = world
    with pytest.raises(Refusal) as refused:
        send(conn, queue, payload_for(conn, item_id, private), private, via="relay-eli", transport_party="eli")
    assert refused.value.code == "transport_party_mismatch"
    assert send(conn, queue, payload_for(conn, item_id, private), private,
                transport_party="dana")["state"] == "applied"


def test_the_answer_must_be_for_the_current_attempt_and_epoch(world):
    conn, queue, item_id, private = world
    for wrong, code in (({"verify_attempt": 1}, "stale"), ({"epoch": 3}, "not_the_owner")):
        payload = payload_for(conn, item_id, private, **wrong)
        with pytest.raises(Refusal) as refused:
            send(conn, queue, payload, private)
        assert refused.value.code == code
        assert row(conn, payload)["status"] == "refused"
        assert row(conn, payload)["receipt"]["code"] == code


# --------------------------------------------------------------------------- #
# age: fresh, grace, expired
# --------------------------------------------------------------------------- #


def test_an_answer_in_the_grace_window_is_held_not_applied_and_not_refused(world):
    conn, queue, item_id, private = world
    payload = payload_for(conn, item_id, private, issued_at=int((NOW - timedelta(hours=30)).timestamp()))
    result = send(conn, queue, payload, private)
    assert result["state"] == "held" and result["item"] is None
    assert result["receipt"]["receipt"]["message"].startswith("needs your validation")
    assert queue.get(item_id).status is WorkStatus.AWAITING_VERIFY, "a late answer never lands on its own"
    assert send(conn, queue, payload, private)["state"] == "held", "and stays held when resent"


def test_the_windows_are_configurable(world, monkeypatch):
    conn, queue, item_id, private = world
    monkeypatch.setenv(envelope.FRESH_ENV_VAR, "3600")
    assert envelope.windows_from_env() == (3600, 1800)
    payload = payload_for(conn, item_id, private, issued_at=int((NOW - timedelta(hours=2)).timestamp()))
    with pytest.raises(Refusal) as refused:
        send(conn, queue, payload, private)
    assert refused.value.code == "envelope_expired"


# --------------------------------------------------------------------------- #
# nonces: replay, races, crashes (S5)
# --------------------------------------------------------------------------- #


def test_the_same_envelope_twice_is_one_answer(world):
    conn, queue, item_id, private = world
    payload = payload_for(conn, item_id, private, verdict={"passed": False, "reason": "not yet"})
    first = send(conn, queue, payload, private)
    again = send(conn, queue, payload, private)
    assert again["state"] == "applied" and again["repeat"]
    assert again["receipt"]["receipt"] == first["receipt"]["receipt"]
    assert queue.get(item_id).verify_failures == 1, "a resend is not a second failing verdict"


def test_a_different_answer_under_a_used_nonce_is_replayed(world):
    conn, queue, item_id, private = world
    payload = payload_for(conn, item_id, private)
    send(conn, queue, payload, private)
    other = {**payload, "verdict": {"passed": False, "reason": "changed my mind"}}
    with pytest.raises(Refusal) as refused:
        send(conn, queue, other, private)
    assert refused.value.code == "replayed"


def test_two_answers_racing_for_one_slot_the_first_wins_and_the_loser_says_who(world):
    conn, queue, item_id, private = world
    winner = payload_for(conn, item_id, private)
    loser = payload_for(conn, item_id, private, verdict={"passed": False, "reason": "no"})
    send(conn, queue, winner, private)
    with pytest.raises(Refusal) as refused:
        send(conn, queue, loser, private)
    assert refused.value.code == "not_terminal"
    stored = row(conn, loser)
    assert stored["status"] == "refused"
    assert stored["receipt"]["winner"] == {"keyId": winner["key_id"], "nonce": winner["nonce"]}
    assert queue.get(item_id).attestation["envelope"]["nonce"] == winner["nonce"]


def test_a_crash_before_the_attestation_resumes_on_retry(world, monkeypatch):
    conn, queue, item_id, private = world
    payload = payload_for(conn, item_id, private, verdict={"passed": False, "reason": "not yet"})
    real = queue.attest

    def crash(*args, **kwargs):
        raise RuntimeError("process died after the nonce row")

    monkeypatch.setattr(queue, "attest", crash)
    with pytest.raises(RuntimeError):
        send(conn, queue, payload, private)
    assert row(conn, payload)["status"] == "received"
    monkeypatch.setattr(queue, "attest", real)
    assert send(conn, queue, payload, private)["state"] == "applied"
    assert queue.get(item_id).verify_failures == 1


def test_a_crash_after_the_attestation_is_success_on_retry_and_applies_once(world, monkeypatch):
    conn, queue, item_id, private = world
    payload = payload_for(conn, item_id, private, verdict={"passed": False, "reason": "not yet"})
    real = envelope._settle

    def crash(conn_, key_id, nonce, status, receipt, at):
        if status == "applied":
            raise RuntimeError("process died after the attestation")
        return real(conn_, key_id, nonce, status, receipt, at)

    monkeypatch.setattr(envelope, "_settle", crash)
    with pytest.raises(RuntimeError):
        send(conn, queue, payload, private)
    assert queue.get(item_id).verify_failures == 1 and row(conn, payload)["status"] == "received"
    monkeypatch.setattr(envelope, "_settle", real)
    assert send(conn, queue, payload, private)["state"] == "applied"
    assert queue.get(item_id).verify_failures == 1, "the resumed answer is not applied a second time"


def test_prune_collects_settled_rows_only(world):
    conn, queue, item_id, private = world
    settled = payload_for(conn, item_id, private)
    send(conn, queue, settled, private)
    stuck = payload_for(conn, item_id, private)
    with conn:
        conn.execute("INSERT INTO answer_nonces(key_id, nonce, digest, item_id, verify_attempt, status, "
                     "via, arrived_at, arrived_epoch, updated_at) VALUES (?, ?, 'd', ?, 0, 'received', "
                     "'r', 't', ?, 't')", (stuck["key_id"], stuck["nonce"], item_id, int(NOW.timestamp())))
    assert envelope.prune(conn, now=NOW + timedelta(hours=35), fresh=86400, grace=43200) == 0
    assert envelope.prune(conn, now=NOW + timedelta(hours=37), fresh=86400, grace=43200) == 1
    assert row(conn, settled) is None and row(conn, stuck)["status"] == "received"


def test_the_answer_path_works_on_every_backend(backend, queue, tmp_path, request):
    """The nonce table on the registry's backend, the attestation on the work
    store's: the carried envelope must survive the store, not only the JSONL one."""
    target = request.getfixturevalue("_pg_dsn") if backend == "postgres" else tmp_path / "r.sqlite3"
    conn = db.connect(target)
    hubid.ensure(conn)
    queue.declare(humans=["dana"])
    queue.envelope_required_for = envelope.required_for(conn)
    private = ed25519.Ed25519PrivateKey.generate()
    humankeys.enrol(conn, human="dana", public_key=pem(private), fingerprint=fingerprint(private),
                    by="op", humans={"dana"}, label="acme-hub")
    item = queue.create("gate", verify=HUMAN)
    claimed = queue.claim(item.id, "hub-worker")
    queue.report_result(item.id, claimed.lease_attempt, WorkStatus.DONE)
    payload = payload_for(conn, item.id, private)
    assert send(conn, queue, payload, private)["state"] == "applied"
    assert send(conn, queue, payload, private)["repeat"]
    stored = queue.get(item.id).attestation
    assert stored["submitted_by"] == "dana" and stored["envelope"]["payload"] == payload
    assert envelope.prune(conn, now=NOW + timedelta(days=3)) == 1


# --------------------------------------------------------------------------- #
# over HTTP
# --------------------------------------------------------------------------- #

KEYS = {"dana": "dana-secret", "hub-worker": "hw-secret", "relay-dana": "rd-secret", "relay-eli": "re-secret"}


def http(client, method, path, actor, body=None):
    raw = json.dumps(body).encode() if body is not None else b""
    ts = str(int(time.time()))
    headers = {"X-AgentCo-Actor": actor, "X-AgentCo-Timestamp": ts,
               "X-AgentCo-Signature": auth.sign(KEYS[actor], method, path, ts, raw),
               "Content-Type": "application/json"}
    return client.request(method, path, content=raw or None, headers=headers)


def test_http_a_relay_answers_reads_its_receipt_and_nothing_else(tmp_path):
    app = create_app(db_path=str(tmp_path / "api.sqlite3"), keys=KEYS, operator="dana", hub_id="acme-hub",
                     work_store=str(tmp_path / "work.jsonl"), sop_store=str(tmp_path / "sops.jsonl"),
                     humans=["dana", "eli"], transports={"relay-dana": "dana", "relay-eli": "eli"})
    client = TestClient(app)
    conn, queue = app.state.conn, app.state.queue
    private = ed25519.Ed25519PrivateKey.generate()
    humankeys.enrol(conn, human="dana", public_key=pem(private), fingerprint=fingerprint(private),
                    by="op", humans={"dana"}, label="acme-hub")
    item = queue.create("ship it", verify=HUMAN)
    claimed = queue.claim(item.id, "hub-worker")
    queue.report_result(item.id, claimed.lease_attempt, WorkStatus.DONE)

    unsigned = {"attestation": {"check": HUMAN["check"], "exit_status": 0, "environment": "laptop",
                                "at": "2026-10-30T00:00:00+00:00",
                                "verdict": {"passed": True, "reason": "seen"}}}
    refused = http(client, "POST", f"/work/{item.id}/attest", "dana", unsigned)
    assert (refused.status_code, refused.json()["code"]) == (403, "envelope_required")

    payload = payload_for(conn, item.id, private, issued_at=int(time.time()))
    body = {"envelope": json.dumps(payload), "signature": envelope.sign(private, payload)}
    assert http(client, "POST", "/answers", "relay-eli", body).json()["code"] == "transport_party_mismatch"
    answered = http(client, "POST", "/answers", "relay-dana", body)
    assert answered.status_code == 200, answered.json()
    assert answered.json()["answer"] == "applied" and answered.json()["item"]["status"] == "done"

    path = f"/answers/{payload['key_id']}/{payload['nonce']}"
    assert http(client, "GET", path, "relay-dana").json()["receipt"]["status"] == "applied"
    assert http(client, "GET", path, "relay-eli").status_code == 404, "someone else's receipt reads as missing"
    assert http(client, "POST", f"/work/{item.id}/attest", "relay-dana", unsigned).json()["code"] == "route_not_allowed"

    late = payload_for(conn, item.id, private, issued_at=int(time.time()) - 30 * 3600)
    held = http(client, "POST", "/answers", "relay-dana",
                {"envelope": json.dumps(late), "signature": envelope.sign(private, late)})
    assert held.status_code == 202 and held.json()["answer"] == "held"
