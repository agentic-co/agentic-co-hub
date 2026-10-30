"""Generate docs/envelope-vectors.json, the published `asop.answer/1` vectors.

    uv run --extra dev python tools/envelope_vectors.py > docs/envelope-vectors.json

The keys are FIXED test keys, published on purpose: never enrol one. Ed25519
signatures are deterministic, so that vector pins the exact signature bytes.
ECDSA signatures are not, so its vector pins one valid low-S signature that a
verifier must accept; a signer's own output will differ and must verify.
`tests/test_envelope.py` checks the committed file against this code.
"""

from __future__ import annotations

import base64
import json
import sys

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519

from agentco import envelope, humankeys

ED25519_SEED = bytes(range(1, 33))
P256_SCALAR = int.from_bytes(bytes(range(33, 65)), "big")

PAYLOAD = {
    "type": envelope.TYPE,
    "hub": "0123456789abcdef0123456789abcdef",
    "item": "w-0001",
    "verify_attempt": 0,
    "stage": None,
    "epoch": 0,
    "verdict": {"passed": True, "reason": "rollback exercised on staging"},
    "choice": None,
    "signer": "dana",
    "key_id": "",
    "nonce": "00112233445566778899aabbccddeeff",
    "issued_at": 1793318400,
}


def test_keys():
    return (ed25519.Ed25519PrivateKey.from_private_bytes(ED25519_SEED),
            ec.derive_private_key(P256_SCALAR, ec.SECP256R1()))


def vector(private, alg: str, signature: str | None = None) -> dict:
    der = private.public_key().public_bytes(serialization.Encoding.DER,
                                            serialization.PublicFormat.SubjectPublicKeyInfo)
    fingerprint = humankeys.fingerprint_of(der)
    payload = {**PAYLOAD, "key_id": humankeys.key_id_of(fingerprint)}
    return {
        "alg": alg,
        "publicKeySpki": base64.b64encode(der).decode("ascii"),
        "fingerprint": fingerprint,
        "payload": payload,
        "signedBytes": envelope.signed_bytes(payload).decode("utf-8"),
        "signature": signature or envelope.sign(private, payload),
    }


def main() -> int:
    ed, p256 = test_keys()
    doc = {
        "type": envelope.TYPE,
        "note": "Test keys are public: never enrol one. signedBytes = type line, LF, canonical JSON.",
        "ed25519PrivateSeedHex": ED25519_SEED.hex(),
        "p256PrivateScalarHex": f"{P256_SCALAR:064x}",
        "vectors": [vector(ed, humankeys.ED25519), vector(p256, humankeys.ECDSA_P256)],
    }
    json.dump(doc, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
