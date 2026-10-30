"""People's signing keys: enrol, revoke, look up (ADR 0008 §3.2, agentco/humankeys.py).

What would fail if the directory were dishonest: a key enrolled for an agent; a
key enrolled without the operator typing the fingerprint read on a second
channel; one public key under two people; a revoked key enrolled again; a key
row on the hub with no event on the feed (or an event with no row); a revoked
key still answering; a second revocation announcing itself again. Synthetic
keys and names only.
"""

from __future__ import annotations

import hashlib
import json

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa

from agentco import cli, db, events, humankeys, hubid
from agentco.errors import Refusal

HUMANS = frozenset({"dana", "eli"})
LABEL = "acme-hub"


def pem(private) -> bytes:
    return private.public_key().public_bytes(serialization.Encoding.PEM,
                                             serialization.PublicFormat.SubjectPublicKeyInfo)


def fingerprint(private) -> str:
    der = private.public_key().public_bytes(serialization.Encoding.DER,
                                            serialization.PublicFormat.SubjectPublicKeyInfo)
    return hashlib.sha256(der).hexdigest()


@pytest.fixture()
def conn(backend, tmp_path, request):
    target = request.getfixturevalue("_pg_dsn") if backend == "postgres" else tmp_path / "r.sqlite3"
    connection = db.connect(target)
    hubid.ensure(connection)
    return connection


def key_events(conn) -> list[tuple[str, dict]]:
    rows = conn.execute("SELECT kind, payload FROM events WHERE kind IN (?, ?) ORDER BY seq",
                        ("HumanKeyEnrolled", "HumanKeyRevoked")).fetchall()
    return [(r["kind"], json.loads(r["payload"])) for r in rows]


def enrol(conn, private, human="dana", **overrides):
    args = dict(human=human, public_key=pem(private), fingerprint=fingerprint(private),
                by="op", humans=HUMANS, label=LABEL)
    args.update(overrides)
    return humankeys.enrol(conn, **args)


@pytest.mark.parametrize("make, alg", [
    (ed25519.Ed25519PrivateKey.generate, "ed25519"),
    (lambda: ec.generate_private_key(ec.SECP256R1()), "ecdsa-p256"),
])
def test_an_enrolled_key_is_stored_and_announced_together(conn, make, alg):
    private = make()
    key = enrol(conn, private)
    assert key.alg == alg and key.human == "dana" and not key.revoked
    assert key.fingerprint == fingerprint(private)
    assert key.key_id == key.fingerprint[:32], "a partition derives the id from its own key"
    assert key.hub_uid == hubid.read(conn)
    assert humankeys.active(conn, key.key_id) == key
    [(kind, payload)] = key_events(conn)
    assert kind == "HumanKeyEnrolled"
    assert payload == {"human": "dana", "keyId": key.key_id, "alg": alg,
                       "fingerprint": key.fingerprint, "publicKey": key.public_key,
                       "hubId": key.hub_uid}


@pytest.mark.parametrize("humans, human, code", [
    (frozenset(), "dana", "humans_undeclared"),
    (HUMANS, "build-agent", "human_required"),
])
def test_only_a_declared_human_gets_a_key(conn, humans, human, code):
    with pytest.raises(Refusal) as refused:
        enrol(conn, ed25519.Ed25519PrivateKey.generate(), human=human, humans=humans)
    assert refused.value.code == code
    assert humankeys.list_keys(conn) == [] and key_events(conn) == []


def test_a_hub_still_named_local_cannot_enrol(conn):
    with pytest.raises(Refusal) as refused:
        enrol(conn, ed25519.Ed25519PrivateKey.generate(), label="local")
    assert refused.value.code == "hub_id_unbound"
    assert humankeys.list_keys(conn) == []


def test_the_typed_fingerprint_must_be_this_keys(conn):
    private, other = ed25519.Ed25519PrivateKey.generate(), ed25519.Ed25519PrivateKey.generate()
    for typed in (fingerprint(other), "", "not-hex"):
        with pytest.raises(Refusal) as refused:
            enrol(conn, private, fingerprint=typed)
        assert refused.value.code == "fingerprint_mismatch"
    assert humankeys.list_keys(conn) == [] and key_events(conn) == []
    # Read aloud in pairs, upper case: the same fingerprint, so it is accepted.
    spoken = ":".join(fingerprint(private)[i:i + 2] for i in range(0, 64, 2)).upper()
    assert enrol(conn, private, fingerprint=spoken).fingerprint == fingerprint(private)


def test_one_key_is_never_two_people_and_never_comes_back(conn):
    private = ed25519.Ed25519PrivateKey.generate()
    key = enrol(conn, private, human="dana")
    with pytest.raises(Refusal) as refused:
        enrol(conn, private, human="eli")
    assert refused.value.code == "key_in_use"
    humankeys.revoke(conn, key_id=key.key_id, by="op")
    with pytest.raises(Refusal) as refused:
        enrol(conn, private, human="dana")
    assert refused.value.code == "key_in_use"
    assert [k.human for k in humankeys.list_keys(conn)] == ["dana"]
    assert [kind for kind, _ in key_events(conn)] == ["HumanKeyEnrolled", "HumanKeyRevoked"]


@pytest.mark.parametrize("data, code", [
    (pem(ec.generate_private_key(ec.SECP384R1())), "key_algorithm_unsupported"),
    (pem(rsa.generate_private_key(public_exponent=65537, key_size=2048)), "key_algorithm_unsupported"),
    (b"-----BEGIN PUBLIC KEY-----\nnot a key\n-----END PUBLIC KEY-----\n", "key_unreadable"),
    (b"\x00\x01garbage", "key_unreadable"),
])
def test_a_key_that_cannot_sign_an_answer_is_refused(conn, data, code):
    with pytest.raises(Refusal) as refused:
        humankeys.enrol(conn, human="dana", public_key=data, fingerprint="0" * 64, by="op",
                        humans=HUMANS, label=LABEL)
    assert refused.value.code == code
    assert humankeys.list_keys(conn) == []


def test_der_is_accepted_like_pem(conn):
    private = ec.generate_private_key(ec.SECP256R1())
    der = private.public_key().public_bytes(serialization.Encoding.DER,
                                            serialization.PublicFormat.SubjectPublicKeyInfo)
    assert enrol(conn, private, public_key=der).alg == "ecdsa-p256"


def test_a_revoked_key_answers_nothing_and_revoking_twice_says_so(conn):
    key = enrol(conn, ed25519.Ed25519PrivateKey.generate())
    after, changed = humankeys.revoke(conn, key_id=key.key_id, by="op", reason="laptop lost")
    assert changed and after.revoked and after.revoke_reason == "laptop lost"
    assert humankeys.active(conn, key.key_id) is None
    assert humankeys.get(conn, key.key_id) is not None, "the row stays: past answers name it"
    again, changed = humankeys.revoke(conn, key_id=key.key_id, by="op")
    assert not changed and again.revoked_at == after.revoked_at
    assert [kind for kind, _ in key_events(conn)] == ["HumanKeyEnrolled", "HumanKeyRevoked"]
    assert key_events(conn)[1][1] == {"human": "dana", "keyId": key.key_id,
                                      "fingerprint": key.fingerprint, "reason": "laptop lost"}


def test_revoking_an_unknown_key_is_refused(conn):
    with pytest.raises(Refusal) as refused:
        humankeys.revoke(conn, key_id="0" * 32, by="op")
    assert refused.value.code == "key_unknown"


def test_removing_a_person_revokes_only_their_live_keys(conn):
    old = enrol(conn, ed25519.Ed25519PrivateKey.generate())
    humankeys.revoke(conn, key_id=old.key_id, by="op")
    live = enrol(conn, ec.generate_private_key(ec.SECP256R1()))
    other = enrol(conn, ed25519.Ed25519PrivateKey.generate(), human="eli")
    revoked = humankeys.revoke_all(conn, human="dana", by="op", reason="left the company")
    assert [k.key_id for k in revoked] == [live.key_id]
    assert humankeys.active(conn, other.key_id) is not None


def test_a_key_row_and_its_event_commit_together(conn, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("feed write failed")

    monkeypatch.setattr(events, "insert", broken)
    with pytest.raises(RuntimeError):
        enrol(conn, ed25519.Ed25519PrivateKey.generate())
    assert humankeys.list_keys(conn) == [], "a key the feed never announced is not enrolled"


def test_the_cli_enrols_lists_and_refuses(tmp_path, monkeypatch, capsys):
    registry = tmp_path / "r.sqlite3"
    monkeypatch.setenv("ASOP_HUMANS", "dana")
    monkeypatch.setenv("AGENTCO_HUB_ID", LABEL)
    hubid.ensure(db.connect(registry))
    private = ed25519.Ed25519PrivateKey.generate()
    key_file = tmp_path / "dana.pub.pem"
    key_file.write_bytes(pem(private))

    assert cli.main(["human-key", "enrol", "dana", str(key_file), "--fingerprint", "ab" * 32,
                     "--db", str(registry)]) == 1
    assert "fingerprint_mismatch" in capsys.readouterr().err

    assert cli.main(["human-key", "enrol", "dana", str(key_file), "--fingerprint",
                     fingerprint(private), "--db", str(registry), "--by", "op", "--json"]) == 0
    [enrolled] = json.loads(capsys.readouterr().out)
    assert enrolled["human"] == "dana" and enrolled["enrolled_by"] == "op"

    assert cli.main(["human-key", "revoke", "--human", "dana", "--db", str(registry), "--json"]) == 0
    capsys.readouterr()
    assert cli.main(["human-key", "list", "--db", str(registry), "--json"]) == 0
    [listed] = json.loads(capsys.readouterr().out)
    assert listed["key_id"] == enrolled["key_id"] and listed["revoked"] is True
