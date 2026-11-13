"""One partition of a person's runner: pull a company hub's gates, answer them signed.

ADR 0008 §1 and §3. A person serving several companies keeps one partition per
company. It holds that company's transport credential and signing key and
nothing else, and it only ever calls OUT: the company hub never calls the
person. This module is that partition's client:

  * `pending()` pulls the vehicles assigned to the person (`GET /work?ready=1`,
    which a transport credential sees filtered to its party) and hands back
    `{label, ref, title, ageSeconds}` per row, plus the original item the
    answer will name;
  * `answer()` reads the item the vehicle verifies, builds the `asop.answer/1`
    payload (docs/envelope.md), signs it with the person's own key and carries it
    to `POST /answers`;
  * every envelope sent is kept in the partition's own state (§3.2: the person
    keeps every envelope they signed), and the hub's generated id is pinned
    once and refused if it ever changes (§3.1).

It builds no protocol: it composes routes and an envelope the hub already
defines. The signing key never leaves `key_path`; the company hub holds only its
public half (`agentco human-key enrol`).
"""

from __future__ import annotations

import json
import os
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from agentco import envelope, humankeys
from agentco.errors import Refusal
from agentco.publish import Registry, RegistryError

PIN_FILE = "partition.json"
SENT_FILE = "sent.jsonl"
#: agentco.verifiers.VEHICLE_MARKER, restated: this side reads only the wire.
VEHICLE_MARKER = "verifies"


def _refuse(code: str, message: str, remediation: str) -> Refusal:
    return Refusal(code=code, message=message, remediation=remediation, http_status=422)


def generate_key(directory: Path, algorithm: str = "ed25519") -> dict:
    """Create the person's signing key for ONE company hub (§3.2: one key per
    person and hub, never reused). Writes `signing-key.pem` (mode 600) and
    `signing-key.pub.pem`; returns the fingerprint to read to the operator on a
    second channel. Refuses to overwrite an existing key."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec, ed25519

    directory.mkdir(parents=True, exist_ok=True)
    private_path, public_path = directory / "signing-key.pem", directory / "signing-key.pub.pem"
    if private_path.exists():
        raise _refuse("key_exists", f"{private_path} already exists",
                      "A key is never reused for another hub and never overwritten; use a new directory.")
    key = ed25519.Ed25519PrivateKey.generate() if algorithm == "ed25519" else ec.generate_private_key(ec.SECP256R1())
    fd = os.open(private_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                       serialization.NoEncryption()))
    public = key.public_key()
    der = public.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    public_path.write_bytes(public.public_bytes(serialization.Encoding.PEM,
                                                serialization.PublicFormat.SubjectPublicKeyInfo))
    fingerprint = humankeys.fingerprint_of(der)
    return {"privateKey": str(private_path), "publicKey": str(public_path), "algorithm": algorithm,
            "fingerprint": fingerprint, "keyId": humankeys.key_id_of(fingerprint)}


def load_key(path: Path):
    from cryptography.hazmat.primitives import serialization

    return serialization.load_pem_private_key(Path(path).read_bytes(), password=None)


def key_id_of_private(private) -> str:
    from cryptography.hazmat.primitives import serialization

    der = private.public_key().public_bytes(serialization.Encoding.DER,
                                            serialization.PublicFormat.SubjectPublicKeyInfo)
    return humankeys.key_id_of(humankeys.fingerprint_of(der))


def _age_seconds(stamp: Optional[str], now: datetime) -> Optional[int]:
    if not stamp:
        return None
    try:
        then = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return max(0, int((now - then).total_seconds()))


def split_ref(ref: str) -> tuple[str, Optional[int]]:
    """`verify:<item>:<attempt>` -> (item, attempt); anything else is an item id."""
    if ref.startswith("verify:"):
        item, _, last = ref[len("verify:"):].rpartition(":")
        if item and last.isdigit():
            return item, int(last)
    return ref, None


class Partition:
    def __init__(self, *, label: str, url: str, actor: str, secret: str, signer: str,
                 key_path: Path, state_dir: Path, show_titles: bool = True, timeout: int = 15):
        self.label, self.signer = label, signer
        self.key_path, self.state_dir = Path(key_path), Path(state_dir)
        self.show_titles = show_titles
        self.hub = Registry(actor, secret, url, timeout=timeout)

    # -- the hub's generated id, pinned (§3.1) -------------------------------

    def hub_id(self) -> str:
        return self.hub._call("GET", "/hub")["hubId"]

    def pinned_hub_id(self) -> Optional[str]:
        try:
            return json.loads((self.state_dir / PIN_FILE).read_text())["hubId"]
        except (OSError, ValueError, KeyError):
            return None

    def pin(self, hub_id: Optional[str] = None) -> str:
        """Pin the hub id this partition signs for, once, against the id the
        operator reads out on a second channel when the key is enrolled."""
        hub_id = hub_id or self.hub_id()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        (self.state_dir / PIN_FILE).write_text(json.dumps({"hubId": hub_id, "label": self.label}))
        return hub_id

    def _checked_hub_id(self) -> str:
        pinned, current = self.pinned_hub_id(), self.hub_id()
        if pinned is None:
            raise _refuse("hub_not_pinned", f"no hub id is pinned for {self.label!r}",
                          "Run `agentco partition pin` and compare the id with the one the operator reads out.")
        if pinned != current:
            raise _refuse("hub_id_changed", f"{self.label!r} now reports hub id {current}, not the pinned {pinned}",
                          "Do not sign. Ask the operator why the id changed (a backup restored as a second "
                          "hub regenerates it), then pin again on purpose.")
        return pinned

    # -- pull ---------------------------------------------------------------

    def pending(self, now: Optional[datetime] = None) -> list[dict]:
        """The switchboard rows for this company: `{label, ref, title, ageSeconds}`
        plus `item`. `label` comes from this partition's configuration, never
        from hub data; titles can be switched off per company."""
        now = now or datetime.now(timezone.utc)
        rows = []
        for vehicle in self.hub._call("GET", "/work", query="?ready=1").get("items", []):
            item = (vehicle.get("metadata") or {}).get(VEHICLE_MARKER)
            if not item:
                continue
            rows.append({"label": self.label, "ref": vehicle.get("natural_key"),
                         "title": vehicle.get("title") if self.show_titles else None,
                         "ageSeconds": _age_seconds(vehicle.get("created_at"), now), "item": item})
        return rows

    # -- answer -------------------------------------------------------------

    def build(self, *, item: str, passed: bool, reason: str, hub_id: str, private,
              choice: Optional[str] = None, stage: Optional[int] = None, epoch: int = 0,
              verify_attempt: Optional[int] = None, issued_at: Optional[int] = None) -> dict:
        if verify_attempt is None:
            verify_attempt = int(self.hub._call("GET", f"/work/{item}")["item"].get("verify_failures") or 0)
        return {"type": envelope.TYPE, "hub": hub_id, "item": item, "verify_attempt": verify_attempt,
                "stage": stage, "epoch": epoch, "verdict": {"passed": bool(passed), "reason": reason},
                "choice": choice, "signer": self.signer, "key_id": key_id_of_private(private),
                "nonce": secrets.token_hex(16), "issued_at": int(time.time()) if issued_at is None else issued_at}

    def answer(self, ref: str, *, passed: bool, reason: str, choice: Optional[str] = None,
               stage: Optional[int] = None, epoch: int = 0) -> dict:
        """Sign and carry one answer. `ref` is the vehicle's `ref`
        (`verify:<item>:<attempt>`) or the item id. The attempt signed is the
        one the vehicle names, so an answer to a superseded vehicle is refused
        `stale` by the hub rather than landing on the new attempt. Returns
        `{status, body, payload}`; a hub refusal is returned, not raised."""
        hub_id = self._checked_hub_id()
        item, attempt = split_ref(ref)
        private = load_key(self.key_path)
        payload = self.build(item=item, passed=passed, reason=reason, choice=choice, stage=stage, epoch=epoch,
                             hub_id=hub_id, private=private, verify_attempt=attempt)
        body = {"envelope": json.dumps(payload), "signature": envelope.sign(private, payload)}
        self._remember(payload, body)
        return {**self.send(body), "payload": payload}

    def send(self, body: dict) -> dict:
        try:
            return {"status": 200, "body": self.hub._call("POST", "/answers", body)}
        except RegistryError as exc:
            return {"status": exc.status, "body": exc.payload}

    def receipt(self, key_id: str, nonce: str) -> dict:
        return self.hub._call("GET", f"/answers/{key_id}/{nonce}")

    def _remember(self, payload: dict, body: dict) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        line = json.dumps({"label": self.label, "payload": payload, "signature": body["signature"]}, sort_keys=True)
        with open(self.state_dir / SENT_FILE, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    def sent(self) -> list[dict]:
        try:
            lines = (self.state_dir / SENT_FILE).read_text().splitlines()
        except OSError:
            return []
        return [json.loads(line) for line in lines if line.strip()]
