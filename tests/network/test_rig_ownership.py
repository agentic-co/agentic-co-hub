"""Run ownership transferred across a revoked key, in the real image.

`rt-agent` speaks for the external store `runtime-a` on hub-a. It files a run
that its store owns and takes the step's lease; then the operator revokes its
key with the real `agentco revoke --apply`, on the host, against the table the
running hub has mounted. No restart: the next request is refused. The declared
admin transfers the run to the hub, which needs nothing from the old owner.
Re-issuing the old key does not let the old owner's in-flight answer land: the
fence and the epoch both moved. The hub's own worker then finishes the step.
"""

from __future__ import annotations

GATE = {"kind": "deterministic", "check": "make check", "max_park_seconds": 900, "on_timeout": "fail"}
ATT = {"check": "make check", "exit_status": 0, "environment": "ci/synthetic", "at": "2026-10-18T00:00:00+00:00"}


def item(rig, item_id):
    status, page = rig.call("hub-a", "dana", "GET", "/work", query="limit=500")
    assert status == 200, page
    return next(i for i in page["items"] if i["id"] == item_id)


def test_transfer_across_a_revoked_key(rig):
    status, sop = rig.call("hub-a", "dana", "POST", "/sops", {
        "title": "rig: one step", "inputs": [], "roles": {"w": {"kind": "agent"}},
        "steps": [{"name": "one", "role": "w", "purpose": "do the one thing", "gate": GATE}]})
    assert status == 200, sop
    sop_id = sop["sop"]["asop_id"]
    assert rig.call("hub-a", "dana", "POST", f"/sops/{sop_id}/activate", {"version": 1})[0] == 200

    status, filed = rig.call("hub-a", "rt-agent", "POST", f"/sops/{sop_id}/run",
                             {"inputs": {}, "bindings": {"w": "rt-agent"}, "ownerStore": "runtime-a"})
    assert status == 200, filed
    run_id, step = filed["run"]["runId"], filed["run"]["steps"][0]["itemId"]
    filed_owner = rig.call("hub-a", "kofi", "GET", f"/runs/{run_id}")[1]["run"]["owner"]
    assert filed_owner["store"] == "runtime-a" and filed_owner["epoch"] == 0
    assert rig.call("hub-a", "kofi", "POST", "/work/pull", {})[1]["state"] == "empty", "not offered to a non-owner"
    status, held = rig.call("hub-a", "rt-agent", "POST", "/work/pull", {"ttlSeconds": 600})
    assert status == 200 and held["item"]["id"] == step, held

    # the runtime is gone: its only key is revoked, and the running hub honours that at once
    rig.revoke("hub-a", "rt-agent")
    status, refused = rig.call("hub-a", "rt-agent", "GET", "/events")
    assert status == 401, refused

    path, body = f"/runs/{run_id}/transfer", {"to": "hub-a", "reason": "the runtime was decommissioned"}
    status, refused = rig.call("hub-a", "kofi", "POST", path, body)
    assert status == 403, "an agent may not transfer a run"
    status, moved = rig.call("hub-a", "dana", "POST", path, body)
    assert status == 200 and moved["epoch"] == 1 and moved["to"] == "hub-a", moved
    voided = item(rig, step)
    assert voided["status"] == "pending" and voided["lease_attempt"] > held["attempt"] and not voided.get("leased_by")

    # a re-issued key does not revive the old owner's lease
    rig.restore_key("hub-a", "rt-agent")
    status, late = rig.call("hub-a", "rt-agent", "POST", f"/work/{step}/report",
                            {"status": "done", "attempt": held["attempt"], "attestation": ATT})
    assert status in (403, 409) and late.get("code") in ("not_the_owner", "work_conflict"), late
    assert rig.call("hub-a", "rt-agent", "POST", "/work/pull", {})[1]["state"] == "empty"

    # the hub works the run under ordinary fencing
    status, pulled = rig.call("hub-a", "kofi", "POST", "/work/pull", {"ttlSeconds": 600})
    assert status == 200 and pulled["item"]["id"] == step and pulled["attempt"] > held["attempt"], pulled
    status, done = rig.call("hub-a", "kofi", "POST", f"/work/{step}/report",
                            {"status": "done", "attempt": pulled["attempt"], "attestation": ATT})
    assert status == 200 and done["item"]["status"] == "done", done

    view = rig.call("hub-a", "ana", "GET", f"/runs/{run_id}")[1]["run"]
    assert view["owner"]["store"] == "hub-a" and view["owner"]["epoch"] == 1
    assert [h["by"] for h in view["owner"]["history"]] == ["rt-agent", "dana"]
    assert [h["epoch"] for h in view["owner"]["history"]] == [0, 1], "ASOP §7.2: filed at 0, first transfer 1"
    feed = rig.call("hub-a", "ana", "GET", "/events", query="kind=RunTransferred")[1]["events"]
    assert [e["payload"]["runId"] for e in feed] == [run_id]
