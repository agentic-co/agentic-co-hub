"""The generated hub id and the attempt fence, on real hubs from the real image.

The two chain hubs run with the default label `local`, so the label cannot tell
them apart; their generated ids must differ, be 128 bits of hex, and survive a
restart. An attest carrying a stale attempt is refused `stale` over a real
socket, whichever durable backend holds the queue.
"""

from __future__ import annotations

import json
import re

import pytest

HEX32 = re.compile(r"^[0-9a-f]{32}$")
CHAIN = ("chain-sqlite", "chain-pg")
GATE = {"kind": "deterministic", "check": "make check", "max_park_seconds": 900, "on_timeout": "fail"}


def att(code):
    return {"check": "make check", "exit_status": code, "environment": "ci/synthetic",
            "at": "2026-10-18T00:00:00+00:00"}


def hub_id(rig, hub):
    done = rig.exec(hub, "python3", "-m", "agentco", "hub-id", "--json")
    assert done.returncode == 0, done.stderr[-400:]
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_the_chain_hubs_share_a_label_and_have_different_generated_ids(rig):
    first = {hub: hub_id(rig, hub) for hub in CHAIN}
    assert {v["label"] for v in first.values()} == {"local"}, "the label is the unbound default on both"
    ids = [v["hubId"] for v in first.values()]
    assert all(HEX32.match(i) for i in ids) and ids[0] != ids[1]
    for hub in CHAIN:
        rig.restart(hub)
        assert hub_id(rig, hub) == first[hub], f"{hub}: the id survives a restart"


@pytest.mark.parametrize("hub", CHAIN)
def test_a_stale_attempt_is_refused_on_a_real_hub(rig, hub):
    # Assigned to `operator`, who holds no open work after the chain proof has run
    # on the same hub: an unassigned item would race the proof's leftovers for
    # whoever pulls next.
    status, filed = rig.call(hub, "dana", "POST", "/work",
                             {"title": "rig: one gated item", "verify": GATE, "assignedAgent": "operator"})
    assert status == 200, filed
    item = filed["item"]["id"]
    status, pulled = rig.call(hub, "operator", "POST", "/work/pull", {"ttlSeconds": 600})
    assert status == 200 and pulled["item"]["id"] == item, pulled
    status, parked = rig.call(hub, "operator", "POST", f"/work/{item}/report",
                              {"status": "done", "attempt": pulled["attempt"], "attestation": att(1)})
    assert status == 200 and parked["item"]["status"] == "verify_failed", parked
    read = parked["item"]["answer_attempt"]

    status, failed = rig.call(hub, "dana", "POST", f"/work/{item}/attest", {"attestation": att(1), "attempt": read})
    assert status == 200 and failed["item"]["answer_attempt"] == read + 1, failed
    status, refused = rig.call(hub, "dana", "POST", f"/work/{item}/attest", {"attestation": att(0), "attempt": read})
    assert (status, refused["code"]) == (409, "stale") and str(read + 1) in refused["message"], refused
    status, done = rig.call(hub, "dana", "POST", f"/work/{item}/attest", {"attestation": att(0), "attempt": read + 1})
    assert status == 200 and done["item"]["status"] == "done", done
    status, again = rig.call(hub, "dana", "POST", f"/work/{item}/attest", {"attestation": att(0), "attempt": read + 1})
    assert (status, again["code"]) == (409, "not_terminal"), again
