"""Federation exactly as built (ADR 0005): push-only, one upstream.

hub-b pushes its divergence digest to hub-a with `agentco digest --deliver
--post --via hub`, the command an operator schedules. A delivery that fails
(bad secret, undeclared child, a partitioned network) marks nothing, so the
pointers it carried ride the next digest that does get through. A reader of
hub-a's feed resumes on its cursor across a partition and a parent restart and
sees every digest exactly once: no loss, no replay.
"""

from __future__ import annotations

ART = "/var/lib/agentco/artifacts"
DIGEST = ("python3", "-m", "agentco", "digest", "--deliver", "--post", "--via", "hub")


def write_artifact(rig, name, text):
    done = rig.exec("hub-b", "sh", "-c", f"mkdir -p {ART} && printf '%s' '{text}' > {ART}/{name}")
    assert done.returncode == 0, done.stderr


def snapshot(rig, name):
    status, receipt = rig.call("hub-b", "worker-b", "POST", "/snapshots",
                               {"artifactUri": f"file://{ART}/{name}", "purpose": f"rig: {name}", "ttlDays": 7})
    assert status == 200, receipt
    return receipt


def digests_since(rig, cursor=None):
    query = "kind=DigestReceived&limit=500" + (f"&since={cursor}" if cursor else "")
    status, page = rig.call("hub-a", "dana", "GET", "/events", query=query)
    assert status == 200, page
    return page["events"], page["nextCursor"]


def test_push_digests_through_auth_failures_a_partition_and_a_restart(rig):
    seen, start = digests_since(rig)
    assert seen == [], "a fresh parent has received nothing"
    for name in ("one.md", "two.md", "three.md"):
        write_artifact(rig, name, "v1")
        snapshot(rig, name)

    # 1. a delivered digest is one DigestReceived on the parent, signed by the child
    write_artifact(rig, "one.md", "v2")
    sent = rig.exec("hub-b", *DIGEST)
    assert sent.returncode == 0 and "Delivered via hub" in sent.stderr, sent.stderr
    [first], c1 = digests_since(rig, start)
    assert first["actor"] == "hub-b" and first["payload"]["meta"]["movedCount"] == 1
    assert "one.md" in first["payload"]["text"]

    # 2. auth failures: a wrong secret is refused at the transport, an undeclared
    #    child is refused by the parent; neither records anything or marks anything
    write_artifact(rig, "two.md", "v2")
    wrong = rig.exec("hub-b", *DIGEST, env={"AGENTCO_PARENT_HUB_SECRET": "0" * 64})
    assert wrong.returncode == 1 and "nothing marked delivered" in wrong.stderr and "401" in wrong.stderr
    undeclared = rig.exec("hub-c", *DIGEST)
    assert undeclared.returncode == 1 and "not_a_declared_federator" in undeclared.stderr, undeclared.stderr
    assert digests_since(rig, c1)[0] == []

    # 3. partition the child; a delivery attempt fails and marks nothing
    rig.disconnect("hub-b")
    try:
        write_artifact(rig, "three.md", "v2")
        cut_off = rig.exec("hub-b", *DIGEST)
        assert cut_off.returncode == 1 and "nothing marked delivered" in cut_off.stderr, cut_off.stderr
        assert digests_since(rig, c1)[0] == []

        # 4. the parent restarts while the child is away; what it holds survives
        rig.restart("hub-a")
        replay, _ = digests_since(rig, start)
        assert [e["uid"] for e in replay] == [first["uid"]]
        assert digests_since(rig, c1)[0] == []
    finally:
        rig.connect("hub-b")

    # 5. rejoin: the next digest carries BOTH pointers that moved while it could
    #    not deliver (no loss), and not the one already delivered (no replay)
    rejoined = rig.exec("hub-b", *DIGEST)
    assert rejoined.returncode == 0, rejoined.stderr
    [second], c2 = digests_since(rig, c1)
    assert second["payload"]["meta"]["movedCount"] == 2
    text = second["payload"]["text"]
    assert "two.md" in text and "three.md" in text and "one.md" not in text

    # 6. and once delivered, never again
    quiet = rig.exec("hub-b", *DIGEST)
    assert quiet.returncode == 0, quiet.stderr
    [third], c3 = digests_since(rig, c2)
    assert third["payload"]["meta"]["movedCount"] == 0
    assert digests_since(rig, c3)[0] == []
    every, _ = digests_since(rig, start)
    assert [e["uid"] for e in every] == [first["uid"], second["uid"], third["uid"]]
    assert [e["payload"]["meta"]["movedCount"] for e in every] == [1, 2, 0]
    assert {e["actor"] for e in every} == {"hub-b"}, "hub-c never got a digest recorded"


def test_federation_is_one_way(rig):
    """Push-only: the parent has no route to file anything on the child, and
    the child's key table does not hold the parent at all."""
    assert "hub-a" not in rig.secrets["hub-b"]
    status, payload = rig.call("hub-b", "worker-b", "GET", "/events", query="kind=DigestReceived")
    assert status == 200 and payload["events"] == [], payload
