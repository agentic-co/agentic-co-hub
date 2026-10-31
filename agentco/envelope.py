"""Signed answers to human gates: `asop.answer/1` (ADR 0008 §3, §3.3, §8).

A person answers a human gate on their own machine. Their partition signs an
envelope with the key enrolled for them on this hub (agentco/humankeys.py), and
a transport credential carries it here. The transport cannot forge an answer;
it carries one. This module is everything between the bytes arriving and the
verdict landing.

**The v1 contract (Decision 1 = A).** Defined here until the spec adopts it.

  * Payload: every field present, none defaulted —
    `type` (`asop.answer/1`), `hub` (this hub's GENERATED id, §3.1), `item` (the
    ORIGINAL item, never the vehicle, S1), `verify_attempt` (the item's
    `verify_failures`, S1), `stage` (the staged check's index, or null for an
    unstaged gate), `epoch` (the run's ownership epoch; 0 outside a run, S2),
    `verdict` `{passed, reason}`, `choice` (a string or null), `signer`,
    `key_id`, `nonce` (32 lowercase hex, N5) and `issued_at` (integer seconds).
    Integers are integers, never booleans; duplicate keys are refused at parse,
    which is why the envelope travels as a JSON STRING rather than an object.
  * Signed bytes (N5): the type line, one `\\n`, then the spec's canonical JSON
    of the payload (`asop.manifest.canonical_json`, §3.10) — the digest
    function, not a second serialization.
  * Signature: base64. Ed25519 as RFC 8032. ECDSA P-256 over SHA-256 as the
    64-byte `r || s` (IEEE P1363, what platform enclaves and WebCrypto emit),
    with s in the lower half of the order: a high-S signature is refused, so
    one payload cannot carry two valid signatures under one key.

**The checks, in order, before anything changes (§3.3):** the transport's party
is the signer; the schema; the envelope names THIS hub; the key is enrolled for
the signer and not revoked at arrival; the signature; the age; the nonce. Then
the queue's own checks — the signer is the gate's named human, the item is
answerable at this `verify_attempt` and `epoch` — and the attestation lands
with `submitted_by` = the person and the envelope beside it.

**Age (Decision 3, S3)**, measured at arrival from `issued_at`: more than 300 s
in the future is `envelope_future`; up to `fresh` (default 24 h) applies;
between `fresh` and `fresh + grace` (grace defaults to half of fresh) is HELD —
neither applied nor refused, the slot kept, nothing about the item touched —
for the owner to confirm with a fresh envelope (S4); older is
`envelope_expired`.

**The grace confirmation (S4).** `held_for` lists a person's held answers by
slot `(item, verify_attempt)` and digest, and never their verdict: the
partition takes the verdict from its OWN stored envelope for that slot, and
with none it shows "the hub reports an answer you did not send" and offers no
confirm. Confirming is an ordinary NEW `asop.answer/1`, fresh, with a new
nonce; when it applies, every held envelope for the same slot is recorded
`expired`, naming the answer that superseded it, and none of their content
touches the item.

**Two stores, no shared transaction (S5).** The nonce table is in the registry
database and the attestation in the work store. The order is: nonce row
committed `received`, then the attestation (keyed `envelope:<key_id>:<nonce>`,
so the queue's own retry rule recognises it), then the row `applied` with its
receipt. A retry of the same `(key_id, nonce, canonical payload)`:

  * row `applied`, `refused` or `held` — the stored outcome, nothing redone;
  * row `received` — the crash case. Attest again under the same key: the queue
    returns the recorded item when the first attempt landed, and lands it when
    it did not. The item having moved on WITHOUT this envelope is the slot's
    normal refusal.

A different payload under a used nonce is `replayed`. When two envelopes race
for one slot, the loser's row is set `refused` carrying the winner's
`(key_id, nonce)`, in the same path that refuses it.

Nonce rows are kept at least `fresh + grace` from arrival (S3), then
collected by `prune`.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from typing import Any, Optional

from agentco import humankeys
from agentco.errors import Refusal

TYPE = "asop.answer/1"

#: Every payload field; all are required (an absent one is a schema refusal).
FIELDS = ("type", "hub", "item", "verify_attempt", "stage", "epoch", "verdict", "choice",
          "signer", "key_id", "nonce", "issued_at")

#: The transport's clock skew allowance (`auth.MAX_SKEW_S`), S3.
MAX_FUTURE_S = 300

FRESH_ENV_VAR = "AGENTCO_ENVELOPE_FRESH_SECONDS"
GRACE_ENV_VAR = "AGENTCO_ENVELOPE_GRACE_SECONDS"
DEFAULT_FRESH_S = 24 * 3600

_NONCE = re.compile(r"^[0-9a-f]{32}$")
_HUB = re.compile(r"^[0-9a-f]{32}$")
#: The order of P-256's group.
_P256_N = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551

try:  # asop-spec 0.6.0 (spec v3.6) exports the normative canonical JSON
    from asop.manifest import canonical_json

    SUPPORTED = True
except ImportError:  # 0.4.x / 0.5.x
    SUPPORTED = False


def _refuse(code: str, message: str, remediation: str, status: int = 422) -> Refusal:
    return Refusal(code=code, message=message, remediation=remediation, http_status=status)


def windows_from_env() -> tuple[int, int]:
    """`(fresh, grace)` in seconds. Grace defaults to half of fresh (Decision 3)."""
    fresh = int(os.environ.get(FRESH_ENV_VAR) or DEFAULT_FRESH_S)
    grace = os.environ.get(GRACE_ENV_VAR)
    return fresh, int(grace) if grace else fresh // 2


# --------------------------------------------------------------------------- #
# parse, canonical bytes, sign, verify
# --------------------------------------------------------------------------- #


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict:
    keys = [k for k, _ in pairs]
    dupes = sorted({k for k in keys if keys.count(k) > 1})
    if dupes:
        raise _refuse("envelope_invalid", f"duplicate key(s) {dupes} in the envelope",
                      "Serialize each field once. Two parsers would read two different answers.")
    return dict(pairs)


def _no_float(text: str) -> None:
    raise ValueError(f"{text} is a float; the canonical form has none")


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def parse(text: Any) -> dict:
    """The payload from the envelope's JSON text, or `envelope_invalid` naming why."""
    if not SUPPORTED:
        raise _refuse("envelope_unsupported",
                      "the installed asop-spec predates its canonical JSON (0.6.0)",
                      "Upgrade asop-spec to 0.6.0 or later to accept signed answers.", 501)
    if not isinstance(text, str):
        raise _refuse("envelope_invalid", "`envelope` is the payload's JSON TEXT, a string",
                      "Send the JSON text you signed, not a parsed object: an object loses "
                      "duplicate keys before anyone can refuse them.", 400)
    try:
        payload = json.loads(text, object_pairs_hook=_no_duplicates, parse_float=_no_float)
    except Refusal:
        raise
    except ValueError as exc:
        raise _refuse("envelope_invalid", f"the envelope is not valid JSON without floats: {exc}",
                      "Send the canonical JSON of the payload; numbers are integers.", 400) from exc
    if not isinstance(payload, dict):
        raise _refuse("envelope_invalid", "the envelope is not a JSON object", "Send the payload object.", 400)
    missing = [f for f in FIELDS if f not in payload]
    extra = sorted(set(payload) - set(FIELDS))
    if missing or extra:
        raise _refuse("envelope_invalid",
                      f"envelope fields: missing {missing}, unknown {extra}",
                      f"An {TYPE} payload has exactly: {', '.join(FIELDS)}. Absent is never a default.",
                      400)
    problems = []
    if payload["type"] != TYPE:
        problems.append(f"type is {payload['type']!r}, not {TYPE!r}")
    if not isinstance(payload["hub"], str) or not _HUB.match(payload["hub"]):
        problems.append("hub is not a 32-hex generated hub id")
    for name in ("item", "signer", "key_id"):
        if not isinstance(payload[name], str) or not payload[name]:
            problems.append(f"{name} is not a non-empty string")
    for name in ("verify_attempt", "epoch", "issued_at"):
        if not _is_int(payload[name]) or payload[name] < 0:
            problems.append(f"{name} is not a non-negative integer")
    if payload["stage"] is not None and (not _is_int(payload["stage"]) or payload["stage"] < 0):
        problems.append("stage is not null or a non-negative integer")
    if payload["choice"] is not None and not isinstance(payload["choice"], str):
        problems.append("choice is not null or a string")
    verdict = payload["verdict"]
    if (not isinstance(verdict, dict) or set(verdict) != {"passed", "reason"}
            or not isinstance(verdict.get("passed"), bool) or not isinstance(verdict.get("reason"), str)):
        problems.append("verdict is not {passed: bool, reason: str}")
    if not isinstance(payload["nonce"], str) or not _NONCE.match(payload["nonce"]):
        problems.append("nonce is not 32 lowercase hex characters")
    if problems:
        raise _refuse("envelope_invalid", "; ".join(problems),
                      f"Fix the payload to the {TYPE} contract (docs/envelope.md).", 400)
    return payload


def signed_bytes(payload: dict) -> bytes:
    """N5: the type line, one newline, the canonical JSON of the payload."""
    return TYPE.encode("ascii") + b"\n" + canonical_json(payload)


def digest(payload: dict) -> str:
    return hashlib.sha256(signed_bytes(payload)).hexdigest()


def sign(private_key, payload: dict) -> str:
    """The reference signer: base64 signature over `signed_bytes(payload)`.

    What a partition runs, and what the published vectors were made with. An
    ECDSA signature is emitted low-S as `r || s`."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec, ed25519
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

    message = signed_bytes(payload)
    if isinstance(private_key, ed25519.Ed25519PrivateKey):
        return base64.b64encode(private_key.sign(message)).decode("ascii")
    r, s = decode_dss_signature(private_key.sign(message, ec.ECDSA(hashes.SHA256())))
    s = min(s, _P256_N - s)
    return base64.b64encode(r.to_bytes(32, "big") + s.to_bytes(32, "big")).decode("ascii")


def verify_signature(key: humankeys.HumanKey, payload: dict, signature: Any) -> None:
    """Refuse `signature_invalid` unless `signature` verifies under `key`."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

    invalid = _refuse("signature_invalid", f"the signature does not verify under key {key.key_id}",
                      "Sign signed_bytes(payload) with the private key enrolled for the signer, "
                      "and send it base64.", 403)
    try:
        raw = base64.b64decode(signature, validate=True) if isinstance(signature, str) else b""
    except (binascii.Error, ValueError):
        raw = b""
    public: Any = serialization.load_der_public_key(base64.b64decode(key.public_key))
    message = signed_bytes(payload)
    try:
        if key.alg == humankeys.ED25519:
            if len(raw) != 64:
                raise invalid
            public.verify(raw, message)
        else:
            r, s = int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big")
            if len(raw) != 64 or not 0 < s <= _P256_N // 2:
                raise invalid
            public.verify(encode_dss_signature(r, s), message, ec.ECDSA(hashes.SHA256()))
    except InvalidSignature as exc:
        raise invalid from exc


# --------------------------------------------------------------------------- #
# the nonce table
# --------------------------------------------------------------------------- #


def _iso(at: datetime) -> str:
    return at.astimezone(timezone.utc).isoformat()


def _row(conn, key_id: str, nonce: str):
    return conn.execute("SELECT * FROM answer_nonces WHERE key_id = ? AND nonce = ?",
                        (key_id, nonce)).fetchone()


def _settle(conn, key_id: str, nonce: str, status: str, receipt: dict, at: datetime) -> dict:
    with conn:
        conn.execute("UPDATE answer_nonces SET status = ?, receipt = ?, updated_at = ? "
                     "WHERE key_id = ? AND nonce = ?",
                     (status, json.dumps(receipt, sort_keys=True), _iso(at), key_id, nonce))
    return receipt


def receipt(conn, key_id: str, nonce: str) -> Optional[dict]:
    """What became of one envelope: its row, for the transport that carried it."""
    row = _row(conn, key_id, nonce)
    if row is None:
        return None
    return {"keyId": row["key_id"], "nonce": row["nonce"], "item": row["item_id"],
            "status": row["status"], "via": row["via"], "arrivedAt": row["arrived_at"],
            "receipt": json.loads(row["receipt"]) if row["receipt"] else None}


def prune(conn, *, now: Optional[datetime] = None, fresh: Optional[int] = None,
          grace: Optional[int] = None) -> int:
    """Collect settled rows older than fresh + grace from arrival (S3).

    A `received` row is never collected: it is a crash nobody has resumed, and
    deleting it would let the same envelope land a second time as new."""
    at = now or datetime.now(timezone.utc)
    default_fresh, default_grace = windows_from_env()
    keep = (fresh if fresh is not None else default_fresh) + (grace if grace is not None else default_grace)
    cutoff = at.timestamp() - keep
    with conn:
        return conn.execute(
            "DELETE FROM answer_nonces WHERE status <> 'received' AND arrived_epoch < ?", (cutoff,)
        ).rowcount


# --------------------------------------------------------------------------- #
# submit
# --------------------------------------------------------------------------- #


def submit(conn, queue, *, envelope: Any, signature: Any, via: str, hub_uid: str,
           transport_party: Optional[str] = None, now: Optional[datetime] = None,
           fresh: Optional[int] = None, grace: Optional[int] = None) -> dict:
    """Check one signed answer and apply it, hold it, or refuse it.

    `via` is the authenticated actor that carried it. `transport_party` is the
    person a transport-class credential acts for, or None for any other actor.
    Returns `{"state": "applied"|"held", "receipt": {...}, "item": item|None}`.
    """
    at = now or datetime.now(timezone.utc)
    default_fresh, default_grace = windows_from_env()
    fresh = default_fresh if fresh is None else fresh
    grace = default_grace if grace is None else grace

    payload = parse(envelope)
    if transport_party is not None and transport_party != payload["signer"]:
        raise _refuse("transport_party_mismatch",
                      f"{via!r} carries answers for {transport_party!r}, and this one is signed by "
                      f"{payload['signer']!r}",
                      "Each person's answers travel on that person's own transport credential.", 403)
    if payload["hub"] != hub_uid:
        raise _refuse("envelope_wrong_hub", f"the envelope names hub {payload['hub']}, not this hub",
                      "Sign for the hub id this hub reports (GET /hub), pinned when the key was "
                      "enrolled. An answer for another hub never lands here.", 403)
    key = humankeys.get(conn, payload["key_id"])
    if key is None or key.human != payload["signer"]:
        raise _refuse("key_unknown", f"no key {payload['key_id']} is enrolled here for "
                      f"{payload['signer']!r}",
                      "Ask this hub's operator to enrol the key (agentco human-key enrol).", 403)
    if key.revoked:  # judged at arrival, never by issued_at
        raise _refuse("key_revoked", f"key {key.key_id} was revoked at {key.revoked_at}",
                      "Answers signed with a revoked key are refused whenever they were signed. "
                      "Enrol a new key and sign again.", 403)
    verify_signature(key, payload, signature)

    age = int(at.timestamp()) - payload["issued_at"]
    if -age > MAX_FUTURE_S:
        raise _refuse("envelope_future",
                      f"issued_at is {-age} s after arrival; at most {MAX_FUTURE_S} s is allowed",
                      "Check the signing machine's clock.", 400)
    if age > fresh + grace:
        raise _refuse("envelope_expired",
                      f"the envelope is {age} s old; answers older than {fresh + grace} s are refused",
                      "Answer again: sign a fresh envelope for the gate's current attempt.", 409)

    payload_digest = digest(payload)
    with conn:
        inserted = conn.execute(
            "INSERT INTO answer_nonces(key_id, nonce, digest, item_id, verify_attempt, status, via, "
            "arrived_at, arrived_epoch, updated_at) VALUES (?, ?, ?, ?, ?, 'received', ?, ?, ?, ?) "
            "ON CONFLICT (key_id, nonce) DO NOTHING",
            (key.key_id, payload["nonce"], payload_digest, payload["item"], payload["verify_attempt"],
             via, _iso(at), int(at.timestamp()), _iso(at)),
        ).rowcount
    if inserted != 1:
        row = _row(conn, key.key_id, payload["nonce"])
        if row["digest"] != payload_digest:
            raise _refuse("replayed", f"nonce {payload['nonce']} was already used for a different answer",
                          "Every envelope needs a fresh 128-bit nonce.", 409)
        if row["status"] != "received":
            return {"state": row["status"], "receipt": receipt(conn, key.key_id, payload["nonce"]),
                    "item": None, "repeat": True}
        # `received`: a crash between the row and the attestation. Resume below.

    if age > fresh:
        stored = _settle(conn, key.key_id, payload["nonce"], "held", {
            "reason": "grace", "ageSeconds": age, "freshSeconds": fresh,
            "message": "needs your validation: sign a fresh envelope to confirm this answer",
        }, at)
        return {"state": "held", "receipt": {**receipt(conn, key.key_id, payload["nonce"]), "receipt": stored},  # pyright: ignore[reportGeneralTypeIssues] -- `_settle` just wrote the row, so receipt() is a dict
                "item": None}

    gate_item = queue.get(payload["item"])
    gate = (gate_item.verify or {}) if gate_item is not None else {}
    verdict = payload["verdict"]
    # `check`, `exit_status`, `environment` and `at` are the hub's, outside the
    # signature (N5); the environment names the key and the carrier.
    attestation = {"check": gate.get("check") or TYPE, "exit_status": 0 if verdict["passed"] else 1,
                   "environment": f"{TYPE} key {key.key_id} via {via}", "at": _iso(at),
                   "verdict": verdict}
    if payload["stage"] is not None:
        attestation["stage"] = payload["stage"]
    carried = {"type": TYPE, "payload": payload, "signature": signature, "key_id": key.key_id,
               "nonce": payload["nonce"], "via": via, "digest": payload_digest}
    try:
        updated = queue.attest(
            payload["item"], attestation, submitted_by=payload["signer"],
            epoch=payload["epoch"], verify_attempt=payload["verify_attempt"],
            idempotency_key=f"envelope:{key.key_id}:{payload['nonce']}", envelope=carried,
        )
    except Refusal as refusal:
        current = queue.get(payload["item"])
        winner = ((current.attestation or {}).get("envelope") if current is not None else None) or {}
        _settle(conn, key.key_id, payload["nonce"], "refused", {
            "code": refusal.code, "message": refusal.message,
            "winner": {"keyId": winner.get("key_id"), "nonce": winner.get("nonce")} if winner else None,
        }, at)
        raise
    if updated is None:
        _settle(conn, key.key_id, payload["nonce"], "refused",
                {"code": "unknown_item", "message": f"no item {payload['item']}", "winner": None}, at)
        raise _refuse("unknown_item", f"no item {payload['item']} on this hub",
                      "Answer the item the gate is on; `item` is the original, never the vehicle.", 404)
    stored = _settle(conn, key.key_id, payload["nonce"], "applied", {
        "item": updated.id, "status": updated.status.value, "verifyFailures": updated.verify_failures,
    }, at)
    _expire_held(conn, payload, key.key_id, at)
    return {"state": "applied", "receipt": {**receipt(conn, key.key_id, payload["nonce"]), "receipt": stored},  # pyright: ignore[reportGeneralTypeIssues] -- `_settle` just wrote the row, so receipt() is a dict
            "item": json.loads(updated.to_json())}


def _expire_held(conn, payload: dict, key_id: str, at: datetime) -> None:
    """S4: an answer applied for a slot ends every held envelope for that slot."""
    superseded = json.dumps({"supersededBy": {"keyId": key_id, "nonce": payload["nonce"]}}, sort_keys=True)
    with conn:
        conn.execute(
            "UPDATE answer_nonces SET status = 'expired', receipt = ?, updated_at = ? "
            "WHERE status = 'held' AND item_id = ? AND verify_attempt = ?",
            (superseded, _iso(at), payload["item"], payload["verify_attempt"]),
        )


def held_for(conn, human: str) -> list[dict]:
    """The held answers signed by `human`'s keys, oldest first: the slot, the
    digest, when it arrived — never the verdict (S4)."""
    key_ids = [k.key_id for k in humankeys.list_keys(conn, human=human)]
    if not key_ids:
        return []
    marks = ", ".join("?" for _ in key_ids)
    rows = conn.execute(
        f"SELECT key_id, nonce, item_id, verify_attempt, digest, arrived_at FROM answer_nonces "
        f"WHERE status = 'held' AND key_id IN ({marks}) ORDER BY arrived_epoch, nonce",
        tuple(key_ids),
    ).fetchall()
    return [{"keyId": r["key_id"], "nonce": r["nonce"], "item": r["item_id"],
             "verifyAttempt": r["verify_attempt"], "digest": r["digest"], "arrivedAt": r["arrived_at"],
             "message": "needs your validation: confirm with a fresh envelope signed from your own record"}
            for r in rows]


def required_for(conn):
    """B1: the predicate a queue holds. True once `human` has ANY key enrolled
    here, revoked ones included — revoking every key of a removed person must
    not reopen the unsigned path for their name."""
    def requires(human: str) -> bool:
        return bool(humankeys.list_keys(conn, human=human))
    return requires
