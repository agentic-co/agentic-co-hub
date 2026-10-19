"""Route allowlist, derived read agent and signed queries, in the real image.

`relay` on hub-a is a transport: its key-table entry carries
`"routes": ["attest", "events"]`. It authenticates, reaches those two routes and
nothing else, and an edit to the table applies on the next request. Every hub
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


def test_the_transport_reaches_only_its_routes(rig):
    status, events = rig.call("hub-a", "relay", "GET", "/events")
    assert status == 200, events
    # an allowed verb reaches its handler: the refusal is the handler's, not the allowlist's
    status, reached = rig.call("hub-a", "relay", "POST", "/work/no-such-item/attest",
                               {"attempt": 1, "attestation": {}})
    assert code(reached) != "route_not_allowed", reached

    for method, path, body, query in (("GET", "/work", None, ""), ("GET", "/work", None, "ready=1"),
                                      ("POST", "/work", {"title": "from a relay"}, ""),
                                      ("POST", "/work/pull", {}, ""), ("GET", "/sops", None, "")):
        status, refused = rig.call("hub-a", "relay", method, path, body, query=query)
        assert status == 403 and code(refused) == "route_not_allowed", (method, path, query, refused)


def test_a_table_edit_applies_on_the_next_request(rig):
    path = rig.keys_dir / "hub-a" / "keys.json"
    original = path.read_text(encoding="utf-8")
    try:
        table = json.loads(original)
        table["relay"]["routes"] = ["attest"]
        path.write_text(json.dumps(table), encoding="utf-8")
        status, refused = rig.call("hub-a", "relay", "GET", "/events")
        assert status == 403 and code(refused) == "route_not_allowed", refused
        # a malformed value fails closed, for that actor only
        table["relay"]["routes"] = "events"
        path.write_text(json.dumps(table), encoding="utf-8")
        assert rig.call("hub-a", "relay", "GET", "/events")[0] == 403
        assert rig.call("hub-a", "kofi", "GET", "/events")[0] == 200
    finally:
        path.write_text(original, encoding="utf-8")
    assert rig.call("hub-a", "relay", "GET", "/events")[0] == 200


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
