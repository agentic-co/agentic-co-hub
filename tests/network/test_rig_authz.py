"""Route allowlist, derived read agent and signed queries, in the real image.

`scribe` on hub-a carries `"routes": ["attest", "events"]`: it authenticates,
reaches those two routes and nothing else, and an edit to the table applies on
the next request. `relay` is the partition transport class (`"class":
"transport"`, owner `dana`): it reaches the hub id, its party's ready work, the
single-item read and the human-key feed, and nothing else. Every hub
runs with `ASOP_REQUIRE_SIGNED_QUERY=1`, so a signature that leaves the query
out is refused over a live socket.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

from agentco import auth


def code(payload):
    return payload.get("code") if isinstance(payload, dict) else None


def test_a_routes_allowlist_reaches_only_its_routes(rig):
    status, events = rig.call("hub-a", "scribe", "GET", "/events")
    assert status == 200, events
    # an allowed verb reaches its handler: the refusal is the handler's, not the allowlist's
    status, reached = rig.call("hub-a", "scribe", "POST", "/work/no-such-item/attest",
                               {"attempt": 1, "attestation": {}})
    assert code(reached) != "route_not_allowed", reached

    for method, path, body, query in (("GET", "/work", None, ""), ("GET", "/work", None, "ready=1"),
                                      ("POST", "/work", {"title": "from a relay"}, ""),
                                      ("POST", "/work/pull", {}, ""), ("GET", "/sops", None, "")):
        status, refused = rig.call("hub-a", "scribe", method, path, body, query=query)
        assert status == 403 and code(refused) == "route_not_allowed", (method, path, query, refused)


def test_a_table_edit_applies_on_the_next_request(rig):
    path = rig.keys_dir / "hub-a" / "keys.json"
    original = path.read_text(encoding="utf-8")
    try:
        table = json.loads(original)
        table["scribe"]["routes"] = ["attest"]
        path.write_text(json.dumps(table), encoding="utf-8")
        status, refused = rig.call("hub-a", "scribe", "GET", "/events")
        assert status == 403 and code(refused) == "route_not_allowed", refused
        # a malformed value fails closed, for that actor only
        table["scribe"]["routes"] = "events"
        path.write_text(json.dumps(table), encoding="utf-8")
        assert rig.call("hub-a", "scribe", "GET", "/events")[0] == 403
        assert rig.call("hub-a", "kofi", "GET", "/events")[0] == 200
    finally:
        path.write_text(original, encoding="utf-8")
    assert rig.call("hub-a", "scribe", "GET", "/events")[0] == 200


def test_a_ready_read_naming_another_agent_is_refused(rig):
    status, refused = rig.call("hub-a", "kofi", "GET", "/work", query="ready=1&agent=ana")
    assert status == 400 and code(refused) == "agent_from_signature", refused
    status, own = rig.call("hub-a", "kofi", "GET", "/work", query="ready=1&agent=kofi")
    assert status == 200, own
    # the declared operator may name any agent
    status, anyone = rig.call("hub-a", "operator", "GET", "/work", query="ready=1&agent=ana")
    assert status == 200, anyone


def test_a_signature_that_leaves_out_the_query_is_refused(rig):
    """The old string, signed without the query, over a live socket."""
    secret = rig.secrets["hub-a"]["kofi"]
    ts = str(int(time.time()))
    request = urllib.request.Request(
        f"{rig.url('hub-a')}/work?ready=1",
        headers={"X-AgentCo-Actor": "kofi", "X-AgentCo-Timestamp": ts,
                 "X-AgentCo-Signature": auth.sign(secret, "GET", "/work", ts, b"")})
    try:
        urllib.request.urlopen(request, timeout=10)
        raise AssertionError("a signature that does not cover the query was accepted")
    except urllib.error.HTTPError as exc:
        assert exc.code == 401
    # and the current form, which the client library signs, is served
    assert rig.call("hub-a", "kofi", "GET", "/work", query="ready=1")[0] == 200


def test_the_partition_transport_class(rig):
    import re

    # the hub id route: 32-hex id and a label, for every actor
    status, hub = rig.call("hub-a", "relay", "GET", "/hub")
    assert status == 200 and re.fullmatch(r"[0-9a-f]{32}", hub["hubId"]) and hub["label"], hub
    assert rig.call("hub-a", "kofi", "GET", "/hub")[1]["hubId"] == hub["hubId"]
    assert rig.call("hub-b", "worker-b", "GET", "/hub")[1]["hubId"] != hub["hubId"]

    # the party filter: dana's item yes; unassigned and kofi's no
    ids = {}
    for name, assignee in (("xr-open", None), ("xr-dana", "dana"), ("xr-kofi", "kofi")):
        # `requires` a capability nobody pulls with: visible to ready reads, never
        # claimable, so these items cannot leak into other rig scenarios' pulls
        body = {"title": name, "requires": ["xr-never"], **({"assignedAgent": assignee} if assignee else {})}
        status, filed = rig.call("hub-a", "operator", "POST", "/work", body)
        assert status == 200, filed
        ids[name] = filed["item"]["id"]
    status, ready = rig.call("hub-a", "relay", "GET", "/work", query="ready=1")
    assert status == 200, ready
    got = {i["title"] for i in ready["items"]}
    assert "xr-dana" in got and not ({"xr-open", "xr-kofi"} & got), got
    status, refused = rig.call("hub-a", "relay", "GET", "/work", query="ready=1&agent=kofi")
    assert status == 400 and code(refused) == "transport_query_refused", refused

    # a hidden item answers like a missing one
    hidden = rig.call("hub-a", "relay", "GET", f"/work/{ids['xr-open']}")
    missing = rig.call("hub-a", "relay", "GET", "/work/xr-ghost")
    assert hidden[0] == missing[0] == 404 and code(hidden[1]) == code(missing[1]) == "work_item_unknown"
    assert hidden[1]["message"].replace(ids["xr-open"], "<id>") == missing[1]["message"].replace("xr-ghost", "<id>")
    assert rig.call("hub-a", "kofi", "GET", f"/work/{ids['xr-open']}")[0] == 200

    # the feed carries human-key kinds only (none emitted yet)
    status, feed = rig.call("hub-a", "relay", "GET", "/events")
    assert status == 200 and all(e["kind"].startswith("HumanKey") for e in feed["events"]), feed

    for method, path, body, query in (("GET", "/work", None, ""), ("POST", "/work", {"title": "x"}, ""),
                                      ("POST", "/work/pull", {}, ""), ("GET", "/sops", None, ""),
                                      ("GET", "/runs", None, ""), ("GET", "/memory", None, ""),
                                      ("POST", "/work/xr-ghost/attest", {"attempt": 1, "attestation": {}}, "")):
        status, refused = rig.call("hub-a", "relay", method, path, body, query=query)
        assert status == 403 and code(refused) == "route_not_allowed", (method, path, refused)
