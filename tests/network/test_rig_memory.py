"""The memory switch, per hub, in the real image (owner decision 4, option B).

hub-a runs with `AGENTCO_MEMORY=on` and a clearance; hub-b leaves the default
(off) and hub-c sets it off explicitly. The off hubs answer every memory route
with the one documented refusal; on hub-a the wall (ADR 0006 §2) holds for an
under-cleared caller over a real socket, and survives a restart.
"""

from __future__ import annotations

import json

import pytest

SECRET_WORD = "zanzibar"  # appears only in the walled item
PROV = {"kind": "run", "ref": "run:rig-0001"}
ITEM = {"label": {"owner": "acme", "level": "internal"}, "layer": "shared",
        "title": "Retry the export once", "body": "A stalled export resumes after one retry.", "provenance": PROV}


@pytest.mark.parametrize("hub,actor", [("hub-b", "worker-b"), ("hub-c", "worker-c")])
def test_an_off_hub_refuses_every_memory_route(rig, hub, actor):
    for method, path, body, query in (
        ("GET", "/memory", None, ""),
        ("POST", "/memory", ITEM, ""),
        ("GET", "/memory/search", None, "q=export"),
        ("GET", "/memory/mem-000000000000", None, ""),
        ("POST", "/memory/mem-000000000000/rate", {"useful": True}, ""),
    ):
        status, payload = rig.call(hub, actor, method, path, body, query=query)
        assert (status, payload.get("code")) == (404, "memory_disabled"), (hub, method, path, payload)
    status, page = rig.call(hub, actor, "GET", "/events", query="kind=MemoryChanged")
    assert status == 200 and page["events"] == []


def test_the_on_hubs_wall_holds_over_the_wire_and_across_a_restart(rig):
    status, walled = rig.call("hub-a", "dana", "POST", "/memory", {
        "label": {"owner": "acme", "level": "confidential"}, "layer": "shared",
        "title": f"The {SECRET_WORD} rollout order", "body": f"Ship {SECRET_WORD} to region two first.",
        "tags": [SECRET_WORD], "provenance": PROV})
    assert status == 200, walled
    walled_id = walled["item"]["item_id"]
    status, open_item = rig.call("hub-a", "kofi", "POST", "/memory", ITEM)
    assert status == 200, open_item

    over = rig.call("hub-a", "kofi", "POST", "/memory", {**ITEM, "label": {"owner": "acme", "level": "confidential"}})
    assert (over[0], over[1].get("code")) == (403, "clearance_insufficient")

    def wall_holds():
        for actor, visible in (("kofi", 1), ("ana", 0)):
            status, page = rig.call("hub-a", actor, "GET", "/memory")
            assert status == 200 and page["count"] == visible and SECRET_WORD not in json.dumps(page)
            status, found = rig.call("hub-a", actor, "GET", "/memory/search", query=f"q={SECRET_WORD}")
            assert status == 200 and found["count"] == 0 and SECRET_WORD not in json.dumps(found)
            hidden = rig.call("hub-a", actor, "GET", f"/memory/{walled_id}")
            missing = rig.call("hub-a", actor, "GET", "/memory/mem-000000000000")
            assert hidden[0] == missing[0]
            assert hidden[1] == json.loads(json.dumps(missing[1]).replace("mem-000000000000", walled_id))
            status, feed = rig.call("hub-a", actor, "GET", "/events", query="kind=MemoryChanged")
            assert walled_id not in json.dumps(feed) and SECRET_WORD not in json.dumps(feed)
        status, page = rig.call("hub-a", "dana", "GET", "/memory")
        assert status == 200 and page["count"] == 2
        status, item = rig.call("hub-a", "dana", "GET", f"/memory/{walled_id}")
        assert status == 200 and item["item"]["body"].startswith("Ship")

    wall_holds()
    rig.restart("hub-a")
    wall_holds()
