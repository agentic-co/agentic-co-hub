"""Gate 2 of v1 (ADR 0011): the control loop closes, across process boundaries.

ADR 0008's milestone: a person answers a human gate from their own side with a
signed envelope, and the company hub records it, pull only. Everything here runs
as SEPARATE OS PROCESSES with nothing in common but sockets:

  * company hub A and company hub B: two real `agentco serve` processes, each
    with its own database, key table, humans registry and generated hub id;
  * the person's partition: `agentco partition ...` subprocesses with their own
    state directory, signing key (generated here, Ed25519) and transport
    credential. The company hub is configured with no address for it, and it
    never calls out: the partition pulls and carries.

Nothing is mocked and no in-process state is shared: the hub verifies a real
signature against a key the operator enrolled through the operator CLI.

What would fail if the loop were dishonest: a gate answered without the person's
key; an answer that lands for another gate, attempt, hub or person; a replay that
applies twice; a flipped verdict keeping its signature; the run not advancing
after a valid answer; a refusal that leaves the item changed.

Every process is a daemon-style child killed in `finally`; every wait has a
deadline, so a failure cannot hang the run. Synthetic names only.
"""

from __future__ import annotations

import base64
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from agentco import auth, envelope, partition
from agentco.publish import Registry, RegistryError

pytestmark = pytest.mark.skipif(not envelope.SUPPORTED,
                                reason="installed asop-spec predates canonical JSON (0.6.0)")

ROOT = Path(__file__).resolve().parents[1]
HUMAN_GATE = {"kind": "human", "check": "a person confirms the rollback was exercised", "verifier": "dana",
              "max_park_seconds": 3600, "on_timeout": "escalate", "escalate_to": "dana"}
PERSON = "dana"


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def clean_env() -> dict:
    """The parent's environment minus anything that configures a hub, so a
    developer's own AGENTCO_*/ASOP_* never leaks into the processes under test."""
    return {k: v for k, v in os.environ.items() if not k.startswith(("AGENTCO_", "ASOP_"))}


def cli(env: dict, *args: str, timeout: float = 60) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "agentco", *args], env=env, cwd=ROOT, capture_output=True,
                          text=True, timeout=timeout)


class Hub:
    """One company hub as a real process: its own directory, database and keys."""

    def __init__(self, root: Path, name: str, secrets: dict[str, str]):
        self.name, self.dir = name, root / name
        self.dir.mkdir()
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.secrets = secrets
        table = {"operator": secrets["operator"], PERSON: secrets[PERSON], "eli": secrets["eli"],
                 "worker": secrets["worker"],
                 # transport credentials: one per person, each acting for that person only
                 f"relay-{PERSON}": {"secret": secrets["relay-dana"], "owner": PERSON, "class": "transport"},
                 "relay-eli": {"secret": secrets["relay-eli"], "owner": "eli", "class": "transport"},
                 "relay-mallory": {"secret": secrets["relay-mallory"], "owner": "mallory", "class": "transport"}}
        keys = self.dir / "keys.json"
        keys.write_text(json.dumps(table))
        keys.chmod(0o600)
        self.env = {**clean_env(), "AGENTCO_REGISTRY_KEYS": str(keys), "AGENTCO_DB": str(self.dir / "hub.sqlite3"),
                    "AGENTCO_REGISTRY_OPERATOR": "operator", "AGENTCO_HUB_ID": f"company-{name}",
                    # `mallory` is deliberately NOT declared; `eli` is a declared human who names no gate here
                    "ASOP_HUMANS": f"{PERSON},eli", "ASOP_VERIFIERS": "",
                    "ASOP_REQUIRE_SIGNED_QUERY": "1", "PYTHONUNBUFFERED": "1"}
        self.proc: subprocess.Popen | None = None

    def start(self) -> "Hub":
        log = open(self.dir / "serve.log", "wb")
        self.proc = subprocess.Popen([sys.executable, "-m", "agentco", "serve", "--port", str(self.port)],
                                     env=self.env, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"hub {self.name} exited: {(self.dir / 'serve.log').read_text()[-600:]}")
            try:
                urllib.request.urlopen(f"{self.url}/events", timeout=2)
            except urllib.error.HTTPError as exc:
                if exc.code == 401:  # serving, key table loaded closed
                    return self
            except OSError:
                pass
            time.sleep(0.2)
        raise RuntimeError(f"hub {self.name} not up after 30 s")

    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    def as_actor(self, actor: str) -> Registry:
        return Registry(actor, self.secrets[actor], self.url, timeout=15)

    def hub_id(self) -> str:
        return json.loads(cli(self.env, "hub-id", "--json").stdout)["hubId"]

    def enrol(self, human: str, public_key: str, fingerprint: str) -> subprocess.CompletedProcess:
        return cli(self.env, "human-key", "enrol", human, public_key, "--fingerprint", fingerprint)

    def pulse(self) -> None:
        done = cli(self.env, "pulse", "--apply")
        assert done.returncode in (0, 1), done.stdout[-400:] + done.stderr[-400:]

    def park_gate(self, title: str) -> dict:
        """File a run of a procedure whose one step carries a human gate, have a
        worker execute the step, and let the pulse route the gate. Everything over
        HTTP to the running hub, as the company's own actors."""
        dana, worker = self.as_actor(PERSON), self.as_actor("worker")
        body = {"title": f"Ship the rollback ({title})", "task_type": f"rollback-{title}",
                "purpose": "roll a release back and prove the rollback works",
                "trigger": "a release is rolled back",
                "roles": {"operator": {"kind": "agent"}},
                "steps": [{"name": "roll back", "role": "operator", "purpose": "roll the release back",
                           "definition_of_done": "the previous release serves traffic",
                           "gate": dict(HUMAN_GATE)}]}
        sop = dana._call("POST", "/sops", body)["sop"]
        dana._call("POST", f"/sops/{sop['asop_id']}/activate", {"version": 1})
        run = dana._call("POST", f"/sops/{sop['asop_id']}/run",
                         {"title": f"rollback run ({title})", "inputs": {}, "bindings": {"operator": "worker"}})["run"]
        item = run["steps"][0]["itemId"]
        pulled = worker._call("POST", "/work/pull", {"ttlSeconds": 600})
        assert pulled["item"]["id"] == item, pulled
        parked = worker._call("POST", f"/work/{item}/report", {"status": "done", "attempt": pulled["attempt"]})
        assert parked["item"]["status"] == "awaiting_verify", parked
        self.pulse()  # routes the gate: files the vehicle assigned to the person
        return {"item": item, "run": run["runId"] if "runId" in run else run.get("run_id") or run.get("id")}

    def item(self, item_id: str) -> dict:
        return self.as_actor("operator")._call("GET", f"/work/{item_id}")["item"]


class Person:
    """The person's partition for one company hub: separate environment, state
    directory and signing key. Everything goes through the `agentco partition`
    CLI, a separate process from the hub."""

    def __init__(self, root: Path, hub: Hub, *, signer: str = PERSON, relay: str = "relay-dana"):
        self.hub = hub
        self.dir = Path(tempfile.mkdtemp(prefix=f"person-{hub.name}-{signer}-", dir=root))
        self.env = {**clean_env(), "AGENTCO_PARTITION_LABEL": f"company-{hub.name}",
                    "AGENTCO_PARTITION_URL": hub.url, "AGENTCO_PARTITION_ACTOR": relay,
                    "AGENTCO_PARTITION_SECRET": hub.secrets[relay], "AGENTCO_PARTITION_SIGNER": signer,
                    "AGENTCO_PARTITION_KEY": str(self.dir / "signing-key.pem"),
                    "AGENTCO_PARTITION_STATE": str(self.dir / "state")}
        made = cli(self.env, "partition", "keygen", str(self.dir))
        assert made.returncode == 0, made.stderr
        self.key = json.loads(made.stdout)
        assert oct((self.dir / "signing-key.pem").stat().st_mode & 0o777) == "0o600"

    def run(self, *args: str) -> subprocess.CompletedProcess:
        return cli(self.env, "partition", *args)

    def json(self, *args: str):
        done = self.run(*args)
        assert done.returncode == 0, done.stderr
        return json.loads(done.stdout)

    def answer(self, ref: str, verdict: str = "pass", reason: str = "exercised on staging", *extra: str):
        done = self.run("answer", ref, verdict, "--reason", reason, *extra)
        return done.returncode, (json.loads(done.stdout) if done.stdout.strip() else None), done.stderr


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    root = tmp_path_factory.mktemp("gate2")
    # Two hubs, two disjoint sets of secrets: nothing one hub knows opens the other.
    import secrets as rnd
    hubs = {n: Hub(root, n, {a: rnd.token_hex(16) for a in
                             ("operator", PERSON, "eli", "worker", "relay-dana", "relay-eli", "relay-mallory")})
            for n in ("a", "b")}
    try:
        for hub in hubs.values():
            hub.start()
        a, b = hubs["a"], hubs["b"]
        # The person makes ONE key per company hub (ADR 0008 §3.2) and hands over the PUBLIC half and
        # fingerprint out of band; the operator of each hub enrols it. Hub B gets its own key so the
        # cross-hub case below uses a key valid on B and absent on A.
        person_a, person_b = Person(root, a), Person(root, b)
        for person in (person_a, person_b):
            hub = person.hub
            enrolled = hub.enrol(PERSON, person.key["publicKey"], person.key["fingerprint"])
            assert enrolled.returncode == 0, enrolled.stderr
            pinned = person.json("pin", hub.hub_id())  # pinned against the id the operator read out
            assert pinned["hubId"] == hub.hub_id()
        yield {"a": a, "b": b, "person_a": person_a, "person_b": person_b, "root": root}
    finally:
        for hub in hubs.values():
            hub.stop()


def test_the_two_hubs_are_separate_processes_with_separate_state(world):
    a, b = world["a"], world["b"]
    assert a.proc.pid != b.proc.pid != os.getpid()
    assert a.hub_id() != b.hub_id(), "generated ids differ even though no label was configured to differ"
    assert a.env["AGENTCO_DB"] != b.env["AGENTCO_DB"] and a.secrets != b.secrets
    # the partition holds no hub's database or key table, only its own credential
    assert not any(k in world["person_a"].env for k in ("AGENTCO_DB", "AGENTCO_REGISTRY_KEYS", "ASOP_HUMANS"))
    # the hub is configured with no address for the person: it can only be pulled from
    assert not any("PARTITION" in k or "PARENT" in k for k in a.env)


# --------------------------------------------------------------------------- #
# steps (a)-(e): the loop itself
# --------------------------------------------------------------------------- #


def test_the_person_answers_a_gate_signed_and_the_company_hub_records_it_and_the_run_advances(world):
    a, person = world["a"], world["person_a"]
    parked = a.park_gate("happy")
    item_id = parked["item"]

    # (a) the run reached a human gate; the item waits on it
    assert a.item(item_id)["status"] == "awaiting_verify"

    # (b) the question reaches the person by PULL: a vehicle on the switchboard, shaped as ADR 0008 §1
    rows = person.json("pending")
    assert len(rows) == 1
    row = rows[0]
    assert set(row) == {"label", "ref", "title", "ageSeconds", "item"}
    assert row["label"] == f"company-{a.name}" and row["ref"] == f"verify:{item_id}:0"
    assert row["title"].startswith("Verify: ") and isinstance(row["ageSeconds"], int) and row["item"] == item_id

    # (c) the person answers; the partition signs with the person's own key
    code, sent, err = person.answer(row["ref"])
    assert code == 0, err
    assert sent["status"] == 200 and sent["body"]["answer"] == "applied", sent
    assert sent["keyId"] == person.key["keyId"]

    # (d) the company hub recorded the verdict as the person, carrying the envelope that proves it
    done = a.item(item_id)
    assert done["status"] == "done"
    attestation = done["attestation"]
    assert attestation["submitted_by"] == PERSON
    assert attestation["verdict"] == {"passed": True, "reason": "exercised on staging"}
    carried = attestation["envelope"]
    assert carried["key_id"] == person.key["keyId"] and carried["via"] == "relay-dana"
    assert carried["payload"]["hub"] == a.hub_id() and carried["payload"]["item"] == item_id
    # the stored signature verifies under the public key the person made, checked here with no hub code
    from cryptography.hazmat.primitives import serialization
    public = serialization.load_pem_public_key((person.dir / "signing-key.pub.pem").read_bytes())
    public.verify(base64.b64decode(carried["signature"]), envelope.signed_bytes(carried["payload"]))

    # the receipt is the person's to read, from the same side
    receipt = person.json("receipt", sent["keyId"], sent["nonce"])
    assert receipt["receipt"]["status"] == "applied"

    # (e) the run advances: the step is done, the run container finishes, and no vehicle is left to answer
    a.pulse()
    run = a.as_actor("operator")._call("GET", f"/runs/{parked['run']}")["run"]
    assert all(step["status"] == "done" for step in run["steps"]), run
    assert person.json("pending") == []
    # the person kept the envelope they signed (ADR 0008 §3.2)
    assert [s["payload"]["nonce"] for s in person.json("sent")] == [sent["nonce"]]


def test_titles_can_be_hidden_per_company_and_the_row_keeps_label_and_age(world):
    a, person = world["a"], world["person_a"]
    parked = a.park_gate("titles")
    hidden = Person.__new__(Person)
    hidden.__dict__.update(person.__dict__)
    hidden.env = {**person.env, "AGENTCO_PARTITION_TITLES": "0"}
    rows = hidden.json("pending")
    assert [r["title"] for r in rows if r["item"] == parked["item"]] == [None]
    assert all(r["label"] and r["ageSeconds"] is not None for r in rows)
    code, _, _ = person.answer(f"verify:{parked['item']}:0")  # leave nothing parked
    assert code == 0


# --------------------------------------------------------------------------- #
# step (f): negative cases, each over HTTP against the running hub
# --------------------------------------------------------------------------- #


def signed_body(person: Person, payload: dict, private=None) -> dict:
    private = private or partition.load_key(person.dir / "signing-key.pem")
    return {"envelope": json.dumps(payload), "signature": envelope.sign(private, payload)}


def payload_for(person: Person, item_id: str, **over) -> dict:
    p = partition.Partition(label="x", url=person.hub.url, actor="relay-dana",
                            secret=person.hub.secrets["relay-dana"], signer=PERSON,
                            key_path=person.dir / "signing-key.pem", state_dir=person.dir / "state")
    private = partition.load_key(person.dir / "signing-key.pem")
    payload = p.build(item=item_id, passed=True, reason="looks right", hub_id=person.hub.hub_id(), private=private)
    payload.update(over)
    return payload


def carry(hub: Hub, relay: str, body: dict) -> tuple[int, dict]:
    try:
        return 200, hub.as_actor(relay)._call("POST", "/answers", body)
    except RegistryError as exc:
        return exc.status, exc.payload


def untouched(hub: Hub, item: str) -> None:
    assert hub.item(item)["status"] == "awaiting_verify", "a refused answer must not move the gate"
    assert not hub.item(item).get("attestation")


def test_a_wrong_key_an_unenrolled_key_and_a_stolen_key_id_are_all_refused(world):
    a, person = world["a"], world["person_a"]
    item = a.park_gate("wrongkey")["item"]
    stranger = Person(world["root"], a, signer=PERSON)  # a key the operator never enrolled
    payload = payload_for(person, item)
    # signed by a key nobody enrolled, naming the enrolled key's id: the signature does not verify
    status, body = carry(a, "relay-dana", signed_body(person, payload, partition.load_key(stranger.dir / "signing-key.pem")))
    assert (status, body["code"]) == (403, "signature_invalid")
    # naming its own, unenrolled key id
    own = payload_for(stranger, item, key_id=stranger.key["keyId"])
    status, body = carry(a, "relay-dana", signed_body(stranger, own))
    assert (status, body["code"]) == (403, "key_unknown")
    untouched(a, item)


def test_an_undeclared_human_cannot_be_enrolled_and_cannot_answer(world):
    a, person = world["a"], world["person_a"]
    item = a.park_gate("mallory")["item"]
    mallory = Person(world["root"], a, signer="mallory", relay="relay-mallory")
    enrol = a.enrol("mallory", mallory.key["publicKey"], mallory.key["fingerprint"])
    assert enrol.returncode == 1 and "human" in enrol.stderr.lower(), enrol.stderr
    # her own transport carries her envelope: no key for her on this hub
    status, body = carry(a, "relay-mallory", signed_body(mallory, payload_for(mallory, item, signer="mallory",
                                                                               key_id=mallory.key["keyId"])))
    assert (status, body["code"]) == (403, "key_unknown")
    # dana's transport carrying an envelope someone else signed
    status, body = carry(a, "relay-dana", signed_body(mallory, payload_for(mallory, item, signer="mallory",
                                                                            key_id=mallory.key["keyId"])))
    assert (status, body["code"]) == (403, "transport_party_mismatch")
    untouched(a, item)
    assert person.run("pending").returncode == 0


def test_a_declared_human_who_is_not_the_gates_verifier_cannot_answer_it(world):
    a = world["a"]
    item = a.park_gate("eli")["item"]
    eli = Person(world["root"], a, signer="eli", relay="relay-eli")
    assert a.enrol("eli", eli.key["publicKey"], eli.key["fingerprint"]).returncode == 0
    status, body = carry(a, "relay-eli", signed_body(eli, payload_for(eli, item, signer="eli", key_id=eli.key["keyId"])))
    assert (status, body["code"]) == (422, "attestation_invalid"), body
    untouched(a, item)
    # and the gate's own person still can
    assert world["person_a"].answer(f"verify:{item}:0")[0] == 0


def test_a_tampered_body_keeps_its_signature_but_not_its_validity(world):
    a, person = world["a"], world["person_a"]
    item = a.park_gate("tamper")["item"]
    payload = payload_for(person, item, verdict={"passed": False, "reason": "broken"})
    body = signed_body(person, payload)
    flipped = json.dumps({**payload, "verdict": {"passed": True, "reason": "broken"}})
    status, refused = carry(a, "relay-dana", {"envelope": flipped, "signature": body["signature"]})
    assert (status, refused["code"]) == (403, "signature_invalid")
    untouched(a, item)


def test_an_envelope_for_another_hub_is_refused_even_under_a_key_valid_there(world):
    a, b, person_b = world["a"], world["b"], world["person_b"]
    item_a = a.park_gate("crosshub")["item"]
    item_b = b.park_gate("crosshub")["item"]
    # a perfectly good answer for hub B, signed with B's key and naming B's id ...
    for_b = payload_for(person_b, item_b)
    body = signed_body(person_b, for_b)
    # ... carried to hub A: wrong hub (and A has never heard of this key)
    status, refused = carry(a, "relay-dana", body)
    assert (status, refused["code"]) == (403, "envelope_wrong_hub")
    # the same bytes with A's id swapped in are no longer signed by anyone
    swapped = {**for_b, "hub": a.hub_id(), "item": item_a}
    status, refused = carry(a, "relay-dana", {"envelope": json.dumps(swapped), "signature": body["signature"]})
    assert (status, refused["code"]) == (403, "key_unknown")
    untouched(a, item_a)
    assert carry(b, "relay-dana", body)[0] == 200, "and it is still a valid answer on the hub it was made for"
    assert b.item(item_b)["status"] == "done"


def test_an_envelope_for_the_wrong_attempt_epoch_or_item_is_refused_and_recorded(world):
    a, person = world["a"], world["person_a"]
    item = a.park_gate("slots")["item"]
    other = a.park_gate("slots-other")["item"]
    cases = [({"verify_attempt": 3}, 409, "stale"),
             ({"epoch": 7}, 403, "not_the_owner"),
             ({"item": "does-not-exist"}, 404, "unknown_item")]
    for over, want_status, want_code in cases:
        payload = payload_for(person, item, **over)
        status, refused = carry(a, "relay-dana", signed_body(person, payload))
        assert (status, refused["code"]) == (want_status, want_code), (over, refused)
        if want_code == "not_the_owner":
            # A transport cannot read GET /runs, so the partition learns the run's epoch from this
            # refusal. It must state it (known-issues, "gate 2: the partition reads the epoch from a refusal").
            assert "ownership epoch 0" in refused["message"], refused
        # the hub kept the refusal beside the nonce: the receipt says refused, and why
        receipt = a.as_actor("relay-dana")._call("GET", f"/answers/{payload['key_id']}/{payload['nonce']}")
        assert receipt["receipt"]["status"] == "refused" and receipt["receipt"]["receipt"]["code"] == want_code
    untouched(a, item)
    # an answer for the other gate does not touch this one, and lands on its own
    assert person.answer(f"verify:{other}:0")[0] == 0
    untouched(a, item)
    assert a.item(other)["status"] == "done"


def test_a_replayed_envelope_applies_once_and_a_reused_nonce_is_refused(world):
    a, person = world["a"], world["person_a"]
    item = a.park_gate("replay")["item"]
    payload = payload_for(person, item)
    body = signed_body(person, payload)
    first = carry(a, "relay-dana", body)
    again = carry(a, "relay-dana", body)
    assert first[0] == again[0] == 200 and first[1]["answer"] == "applied"
    assert again[1]["receipt"]["status"] == "applied", "the retry reads back the stored outcome"
    attestations = a.item(item)["attestation"]
    assert attestations["envelope"]["nonce"] == payload["nonce"]
    # same nonce, different answer: replayed
    changed = payload_for(person, item, nonce=payload["nonce"], verdict={"passed": False, "reason": "no"})
    status, refused = carry(a, "relay-dana", signed_body(person, changed))
    assert (status, refused["code"]) == (409, "replayed")
    assert a.item(item)["status"] == "done" and a.item(item)["verify_failures"] == 0
    # a fresh envelope for the answered gate is a second answer: refused, first answer stands
    second = payload_for(person, item)
    status, refused = carry(a, "relay-dana", signed_body(person, second))
    assert (status, refused["code"]) == (409, "not_terminal")
    assert a.item(item)["attestation"]["envelope"]["nonce"] == payload["nonce"]


def test_a_revoked_key_is_refused_at_arrival_and_an_unsigned_attest_is_closed(world):
    a = world["a"]
    person = Person(world["root"], a)
    assert a.enrol(PERSON, person.key["publicKey"], person.key["fingerprint"]).returncode == 0
    person.json("pin", a.hub_id())
    item = a.park_gate("revoked")["item"]
    payload = payload_for(person, item, key_id=person.key["keyId"])
    body = signed_body(person, payload)  # signed while the key was live
    revoked = cli(a.env, "human-key", "revoke", person.key["keyId"], "--reason", "laptop lost")
    assert revoked.returncode == 0, revoked.stderr
    status, refused = carry(a, "relay-dana", body)
    assert (status, refused["code"]) == (403, "key_revoked")
    # B1: with keys enrolled, the person's own HMAC key can no longer write the verdict unsigned
    unsigned = {"attestation": {"check": HUMAN_GATE["check"], "exit_status": 0, "environment": "laptop",
                                "at": "2026-11-01T00:00:00+00:00", "verdict": {"passed": True, "reason": "seen"}}}
    try:
        a.as_actor(PERSON)._call("POST", f"/work/{item}/attest", unsigned)
        raise AssertionError("the unsigned path must be closed")
    except RegistryError as exc:
        assert exc.payload["code"] == "envelope_required"
    untouched(a, item)


def test_the_partition_refuses_to_sign_for_a_hub_whose_id_changed(world):
    a, person = world["a"], world["person_a"]
    item = a.park_gate("pin")["item"]
    bogus = Person.__new__(Person)
    bogus.__dict__.update(person.__dict__)
    bogus.env = {**person.env, "AGENTCO_PARTITION_STATE": str(world["root"] / "elsewhere")}
    code, _, err = bogus.answer(f"verify:{item}:0")
    assert code == 1 and "hub_not_pinned" in err
    bogus.json("pin", "0" * 32)
    code, _, err = bogus.answer(f"verify:{item}:0")
    assert code == 1 and "hub_id_changed" in err
    untouched(a, item)
    assert person.answer(f"verify:{item}:0")[0] == 0


def test_the_unsigned_transport_cannot_read_the_queue_it_is_only_a_carrier(world):
    a = world["a"]
    relay = a.as_actor("relay-dana")
    with pytest.raises(RegistryError) as refused:
        relay._call("GET", "/work")
    assert refused.value.payload["code"] == "route_not_allowed"
    assert auth.KEYS_ENV_VAR  # the credential table is the hub's own, not the partition's


def test_every_refusal_above_is_on_the_company_hubs_own_record(world):
    """Pre-nonce refusals write no nonce row, so the record of them is the hub's
    `calls` table: one row per request with the verb, the carrier, the HTTP status
    and the machine code. Read from the hub's database file by this process, which
    shares nothing else with it. (Runs after the cases it audits, by file order.)"""
    import sqlite3

    conn = sqlite3.connect(f"file:{world['a'].env['AGENTCO_DB']}?mode=ro", uri=True, timeout=10)
    try:
        rows = conn.execute("SELECT verb, actor, status, code FROM calls WHERE status >= 400").fetchall()
    finally:
        conn.close()
    answers = {(actor, code) for verb, actor, _, code in rows if verb == "answer_submit"}
    for expected in (("relay-dana", "signature_invalid"), ("relay-dana", "key_unknown"),
                     ("relay-dana", "transport_party_mismatch"), ("relay-mallory", "key_unknown"),
                     ("relay-dana", "envelope_wrong_hub"), ("relay-dana", "stale"),
                     ("relay-dana", "not_the_owner"), ("relay-dana", "unknown_item"),
                     ("relay-dana", "replayed"), ("relay-dana", "not_terminal"), ("relay-dana", "key_revoked")):
        assert expected in answers, f"no recorded refusal {expected}: {sorted(answers)}"
    assert any(verb == "attest" and code == "envelope_required" for verb, _, _, code in rows)
