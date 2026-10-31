"""People's signing keys on this hub: enrol, revoke, look up (ADR 0008 §3.2).

An answer to a human gate will arrive as an envelope the person signed on their
own machine. The hub can only check that signature against a public key it was
given, so this module is the directory of those keys, and the only thing in the
plane that adds to it or takes from it.

What it holds is the PUBLIC half only. A per-person shared secret was rejected
in 0008: a hub holding it could write the person's verdict itself.

**There is no enrolment verb on the wire.** The operator enrols a key on the
hub's own machine (`agentco human-key enrol`), from a public key the person
handed over out of band, and must type the fingerprint the person read out on a
second channel. A key arriving over the network would be a key whoever holds the
network can choose.

The rules, each a refusal before anything is written:

  * the person is a declared human (`ASOP_HUMANS`); undeclared refuses every
    name, because a key for an agent would let that agent answer a human gate;
  * the hub has a generated id and a configured name that is not `local`: the
    key is stored beside the id it was enrolled against, and an envelope must
    name it (§3.1);
  * the key is Ed25519, or ECDSA P-256 so a platform enclave or a hardware
    token can hold it; the algorithm is bound to the key, never chosen later;
  * the typed fingerprint matches the key;
  * the key has never been enrolled here, under anyone, revoked or not: one
    public key under two people is two people who can answer as each other,
    and a revoked key that comes back is a revocation undone without a record.

Every enrolment and revocation is an event on this hub's feed
(`HumanKeyEnrolled`, `HumanKeyRevoked`), committed in the same transaction as
the row, because the person's partition compares the keys the hub reports with
its own and an unannounced key is the substitution it is looking for.

**Revocation is judged at arrival** by whoever checks an envelope (`active`),
never by the envelope's own `issued_at`. A revoked row is kept: answers it
signed before revocation stay valid history and must keep naming a key.

Identifiers. The fingerprint is the SHA-256 of the key's DER
SubjectPublicKeyInfo, lowercase hex, which any toolkit reproduces
(`openssl pkey -pubin -in key.pem -outform DER | shasum -a 256`). The `key_id`
an envelope carries is its first 32 characters, so a partition derives it from
its own key without asking the hub.

Needs the `cryptography` package (the `envelope` extra) to read and check a
key. Without it, enrolment is refused rather than storing a key nobody parsed.
"""

from __future__ import annotations

import base64
import hashlib
import re
from dataclasses import asdict, dataclass
from typing import Iterable, Optional

from agentco import events, hubid
from agentco.errors import Refusal

ED25519 = "ed25519"
ECDSA_P256 = "ecdsa-p256"
ALGORITHMS = (ED25519, ECDSA_P256)

_FINGERPRINT = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class HumanKey:
    key_id: str
    human: str
    alg: str
    public_key: str  # base64 of the DER SubjectPublicKeyInfo
    fingerprint: str
    hub_uid: str
    enrolled_by: str
    enrolled_at: str
    revoked_by: Optional[str] = None
    revoked_at: Optional[str] = None
    revoke_reason: Optional[str] = None

    @property
    def revoked(self) -> bool:
        return self.revoked_at is not None

    def to_dict(self) -> dict:
        return {**asdict(self), "revoked": self.revoked}


def _refuse(code: str, message: str, remediation: str, status: int = 422) -> Refusal:
    return Refusal(code=code, message=message, remediation=remediation, http_status=status)


def parse_public_key(data: bytes) -> tuple[str, bytes]:
    """`(alg, der)` for a PEM or DER public key, or a refusal naming why not.

    The algorithm comes from the key, so it cannot be declared one way and used
    another. ECDSA is accepted on P-256 only; a key on any other curve, or of any
    other type, is refused rather than stored for a verifier that cannot use it.
    """
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec, ed25519
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise _refuse(
            "crypto_unavailable",
            "reading a signing key needs the `cryptography` package, which is not installed",
            "Install the envelope extra: `pip install 'agentco[envelope]'`.",
            500,
        ) from exc
    try:
        key = (serialization.load_pem_public_key(data) if data.lstrip().startswith(b"-----")
               else serialization.load_der_public_key(data))
    except (ValueError, TypeError) as exc:
        raise _refuse(
            "key_unreadable",
            f"not a PEM or DER public key: {exc}",
            "Export the PUBLIC half as SubjectPublicKeyInfo, e.g. `openssl pkey -in key.pem -pubout`.",
        ) from exc
    if isinstance(key, ed25519.Ed25519PublicKey):
        alg = ED25519
    elif isinstance(key, ec.EllipticCurvePublicKey) and isinstance(key.curve, ec.SECP256R1):
        alg = ECDSA_P256
    else:
        raise _refuse(
            "key_algorithm_unsupported",
            f"a {type(key).__name__} key cannot sign an answer here",
            f"Use one of: {', '.join(ALGORITHMS)} (ECDSA on P-256 only).",
        )
    der = key.public_bytes(serialization.Encoding.DER,
                           serialization.PublicFormat.SubjectPublicKeyInfo)
    return alg, der


def fingerprint_of(der: bytes) -> str:
    return hashlib.sha256(der).hexdigest()


def key_id_of(fingerprint: str) -> str:
    return fingerprint[:32]


def _normalise_fingerprint(typed: str) -> str:
    """What the operator typed, with the separators people read aloud removed."""
    return re.sub(r"[\s:]", "", typed or "").lower()


def _row(row) -> HumanKey:
    return HumanKey(**{field: row[field] for field in HumanKey.__dataclass_fields__})


def get(conn, key_id: str) -> Optional[HumanKey]:
    row = conn.execute("SELECT * FROM human_keys WHERE key_id = ?", (key_id,)).fetchone()
    return _row(row) if row else None


def active(conn, key_id: str) -> Optional[HumanKey]:
    """The key if it is enrolled and not revoked NOW — the check at arrival."""
    key = get(conn, key_id)
    return key if key is not None and not key.revoked else None


def list_keys(conn, *, human: Optional[str] = None) -> list[HumanKey]:
    if human is None:
        rows = conn.execute("SELECT * FROM human_keys ORDER BY enrolled_at, key_id").fetchall()
    else:
        rows = conn.execute("SELECT * FROM human_keys WHERE human = ? ORDER BY enrolled_at, key_id",
                            (human,)).fetchall()
    return [_row(r) for r in rows]


def enrol(conn, *, human: str, public_key: bytes, fingerprint: str, by: str,
          humans: Iterable[str], label: Optional[str]) -> HumanKey:
    """Enrol one person's public key on this hub, or refuse before writing.

    `humans` is the declared-human registry; `label` the hub's configured name.
    Both are passed in rather than read here, so the caller states what it was
    configured with and a test can say it without touching the environment.
    """
    humans = frozenset(humans)
    if not humans:
        raise _refuse(
            "humans_undeclared",
            "no humans are declared on this hub, so no key can be bound to a person",
            "Declare the people who answer gates in ASOP_HUMANS, then enrol.",
            409,
        )
    if human not in humans:
        raise _refuse(
            "human_required",
            f"{human!r} is not a declared human; a key for anyone else could answer a human gate",
            "Enrol keys only for names in ASOP_HUMANS, spelled exactly as declared.",
            403,
        )
    hub_uid = hubid.require_bound(conn, label)
    alg, der = parse_public_key(public_key)
    actual = fingerprint_of(der)
    typed = _normalise_fingerprint(fingerprint)
    if not _FINGERPRINT.match(typed) or typed != actual:
        raise _refuse(
            "fingerprint_mismatch",
            "the fingerprint typed does not match the key file",
            "Ask the person to read their key's SHA-256 fingerprint on a channel other than the "
            "one that carried the key, and type exactly that. A mismatch is the substitution this "
            "check exists to catch: do not copy the fingerprint from the key file.",
        )
    key = HumanKey(
        key_id=key_id_of(actual), human=human, alg=alg,
        public_key=base64.b64encode(der).decode("ascii"), fingerprint=actual,
        hub_uid=hub_uid, enrolled_by=by, enrolled_at=events.now_iso(),
    )
    with conn:
        inserted = conn.execute(
            "INSERT INTO human_keys(key_id, human, alg, public_key, fingerprint, hub_uid, "
            "enrolled_by, enrolled_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (key_id) DO NOTHING",
            (key.key_id, key.human, key.alg, key.public_key, key.fingerprint, key.hub_uid,
             key.enrolled_by, key.enrolled_at),
        ).rowcount
        if inserted == 1:
            events.insert(conn, kind="HumanKeyEnrolled", actor=by, payload={
                "human": key.human, "keyId": key.key_id, "alg": key.alg,
                "fingerprint": key.fingerprint, "publicKey": key.public_key, "hubId": key.hub_uid,
            })
    if inserted != 1:
        existing = get(conn, key.key_id)
        assert existing is not None  # the insert only fails on a row that exists
        raise _refuse(
            "key_in_use",
            f"this key was already enrolled here for {existing.human!r}"
            + (" and revoked" if existing and existing.revoked else ""),
            "A key is enrolled once, for one person, and never again after revocation. Have the "
            "person generate a new key for this hub.",
            409,
        )
    return key


def revoke(conn, *, key_id: str, by: str, reason: Optional[str] = None) -> tuple[HumanKey, bool]:
    """Revoke one key. Returns `(key, changed)`: revoking a revoked key changes
    nothing and announces nothing, and says so rather than pretending it acted."""
    if get(conn, key_id) is None:
        raise _refuse("key_unknown", f"no key {key_id!r} is enrolled on this hub",
                      "List the enrolled keys with `agentco human-key list`.", 404)
    at = events.now_iso()
    with conn:
        changed = conn.execute(
            "UPDATE human_keys SET revoked_by = ?, revoked_at = ?, revoke_reason = ? "
            "WHERE key_id = ? AND revoked_at IS NULL",
            (by, at, reason, key_id),
        ).rowcount
        if changed == 1:
            key = get(conn, key_id)
            assert key is not None  # checked above, and nothing deletes keys
            events.insert(conn, kind="HumanKeyRevoked", actor=by, payload={
                "human": key.human, "keyId": key.key_id, "fingerprint": key.fingerprint,
                "reason": reason,
            })
    revoked = get(conn, key_id)
    assert revoked is not None  # checked above, and nothing deletes keys
    return revoked, changed == 1


def revoke_all(conn, *, human: str, by: str, reason: Optional[str] = None) -> list[HumanKey]:
    """Revoke every live key of one person, for removal from the company
    (Decision 4). Returns the keys this call revoked; their gates are untouched,
    because gates name the person, not a key."""
    revoked = []
    for key in list_keys(conn, human=human):
        if not key.revoked:
            after, changed = revoke(conn, key_id=key.key_id, by=by, reason=reason)
            if changed:
                revoked.append(after)
    return revoked
