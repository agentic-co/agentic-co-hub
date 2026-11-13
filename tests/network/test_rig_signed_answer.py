"""Gate 2 on the Docker rig: a person answers a gate from outside the hub's network.

The hub is `hub-a`, a container from the real image on the rig's bridge network,
with its own database, key table and humans registry
(`AGENTCO_HUMANS=dana,fran`). The person's partition is a SEPARATE PROCESS ON THE
HOST, on a different network, with its own state directory, a freshly generated
Ed25519 signing key and one transport credential. It reaches the hub only through
the hub's published loopback port, and only ever pulls and carries: the hub has no
address for it. The operator steps (enrol the key, read the hub id, run the pulse)
are the operator's own CLI run inside the container, as on a real deployment.

`chain-sqlite` is the second company hub: its id is what a cross-hub answer would name.

For the same loop between two physical machines, see
docs/runbooks/first-signed-answer-loop.md.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from agentco import envelope, partition
from tests.network.rig import COMPOSE, ROOT

HUB, OTHER = "hub-a", "chain-sqlite"
GATE = {"kind": "human", "check": "a person confirms the rollback was exercised", "verifier": "fran",
        "max_park_seconds": 3600, "on_timeout": "escalate", "escalate_to": "fran"}


def hub_cli(rig, hub, *args, stdin=None):
    return subprocess.run(["docker", "compose", "-p", rig.project, "-f", str(COMPOSE), "exec", "-T", hub,
                           "python3", "-m", "agentco", *args], capture_output=True, text=True, input=stdin,
                          timeout=120, env=rig.env)


def host_cli(env, *args):
    return subprocess.run([sys.executable, "-m", "agentco", "partition", *args], capture_output=True, text=True,
                          timeout=60, cwd=ROOT, env=env)


def test_a_person_outside_the_hubs_network_answers_a_gate_and_the_hub_records_it(rig, tmp_path):
    # --- the person, on the host: own key, own state, one transport credential
    made = partition.generate_key(tmp_path / "fran")
    env = {**{k: v for k, v in os.environ.items() if not k.startswith(("AGENTCO_", "ASOP_"))},
           "AGENTCO_PARTITION_LABEL": "rig-company", "AGENTCO_PARTITION_URL": rig.url(HUB),
           "AGENTCO_PARTITION_ACTOR": "relay-fran", "AGENTCO_PARTITION_SECRET": rig.secrets[HUB]["relay-fran"],
           "AGENTCO_PARTITION_SIGNER": "fran", "AGENTCO_PARTITION_KEY": made["privateKey"],
           "AGENTCO_PARTITION_STATE": str(tmp_path / "fran-state")}

    # --- the operator, inside the hub: enrol the PUBLIC key, read the id out
    public = Path(made["publicKey"]).read_text()
    put = subprocess.run(["docker", "compose", "-p", rig.project, "-f", str(COMPOSE), "exec", "-T", HUB, "sh", "-c",
                          "cat > /tmp/fran.pub.pem"], input=public, capture_output=True, text=True, env=rig.env,
                         timeout=60)
    assert put.returncode == 0, put.stderr
    enrolled = hub_cli(rig, HUB, "human-key", "enrol", "fran", "/tmp/fran.pub.pem",
                       "--fingerprint", made["fingerprint"])
    assert enrolled.returncode == 0, enrolled.stderr
    hub_id = json.loads(hub_cli(rig, HUB, "hub-id", "--json").stdout)["hubId"]
    other_id = json.loads(hub_cli(rig, OTHER, "hub-id", "--json").stdout)["hubId"]
    assert hub_id != other_id
    assert json.loads(host_cli(env, "pin", hub_id).stdout)["hubId"] == hub_id

    # --- (a) the company files a gated item, a worker executes it, the pulse routes the gate
    status, filed = rig.call(HUB, "dana", "POST", "/work", {"title": "rig: signed answer", "verify": GATE,
                                                            "assignedAgent": "courier"})
    assert status == 200, filed
    item = filed["item"]["id"]
    status, pulled = rig.call(HUB, "courier", "POST", "/work/pull", {"ttlSeconds": 600})
    assert status == 200 and pulled["item"]["id"] == item, pulled
    status, parked = rig.call(HUB, "courier", "POST", f"/work/{item}/report",
                              {"status": "done", "attempt": pulled["attempt"]})
    assert status == 200 and parked["item"]["status"] == "awaiting_verify", parked
    assert hub_cli(rig, HUB, "pulse", "--apply").returncode in (0, 1)

    # --- (b) the question reaches the person by pull
    rows = json.loads(host_cli(env, "pending").stdout)
    assert [r["item"] for r in rows] == [item] and rows[0]["ref"] == f"verify:{item}:0"

    # --- (f) refusals first: a tampered body, another hub's id, then a stranger's key
    private = partition.load_key(Path(made["privateKey"]))
    probe = partition.Partition(label="x", url=rig.url(HUB), actor="relay-fran",
                                secret=rig.secrets[HUB]["relay-fran"], signer="fran",
                                key_path=Path(made["privateKey"]), state_dir=tmp_path / "probe")

    def carry(payload, signature=None):
        out = probe.send({"envelope": json.dumps(payload), "signature": signature or envelope.sign(private, payload)})
        return out["status"], out["body"].get("code")

    good = probe.build(item=item, passed=False, reason="broken", hub_id=hub_id, private=private)
    flipped = {**good, "verdict": {"passed": True, "reason": "broken"}}
    assert carry(flipped, envelope.sign(private, good)) == (403, "signature_invalid")
    assert carry(probe.build(item=item, passed=True, reason="ok", hub_id=other_id, private=private)) == \
        (403, "envelope_wrong_hub")
    stranger = partition.generate_key(tmp_path / "stranger")
    forged = probe.build(item=item, passed=True, reason="ok", hub_id=hub_id,
                         private=partition.load_key(Path(stranger["privateKey"])))
    assert carry(forged, envelope.sign(partition.load_key(Path(stranger["privateKey"])), forged)) == \
        (403, "key_unknown")
    status, still = rig.call(HUB, "operator", "GET", f"/work/{item}")
    assert still["item"]["status"] == "awaiting_verify", "no refusal moved the gate"

    # --- (c)(d) the person answers; the hub records it as the person, with the envelope
    done = host_cli(env, "answer", rows[0]["ref"], "pass", "--reason", "exercised on staging")
    assert done.returncode == 0, done.stderr
    sent = json.loads(done.stdout)
    assert sent["status"] == 200 and sent["body"]["answer"] == "applied"
    status, after = rig.call(HUB, "operator", "GET", f"/work/{item}")
    attestation = after["item"]["attestation"]
    assert after["item"]["status"] == "done" and attestation["submitted_by"] == "fran"
    assert attestation["envelope"]["key_id"] == made["keyId"] and attestation["envelope"]["via"] == "relay-fran"

    # --- replay of the very same signed bytes: one answer, the stored outcome
    again = probe.send({"envelope": json.dumps(attestation["envelope"]["payload"]),
                        "signature": attestation["envelope"]["signature"]})
    assert again["status"] == 200 and again["body"]["receipt"]["status"] == "applied"

    # --- (e) the gate is closed and nothing is left to answer
    assert hub_cli(rig, HUB, "pulse", "--apply").returncode in (0, 1)
    assert json.loads(host_cli(env, "pending").stdout) == []
