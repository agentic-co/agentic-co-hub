"""The change feed as the synchronisation substrate for a fleet of harnesses.

A harness has two standing questions — *is there work for me* and *have the
procedures changed* — and until these kinds existed the feed could answer
neither. So every harness asked the library and the queue directly, on a timer,
and both answer by reading everything they hold. The scans were never a careless
query; they were what a subscriber is forced into when the feed cannot tell it
that nothing has changed.

What is defended here:

  * **Every write a subscriber must react to reaches the feed**: a new version,
    an activation, a retirement, and work becoming available.
  * **The payload is enough to decide with.** `WorkAvailable` carries `requires`
    and `assignedAgent` precisely so a harness can ignore work that is not its
    own WITHOUT pulling the queue. A kind that forced a pull to find out would
    move the scan rather than remove it.
  * **A run announces once, not once per step.** The steps arrive blocked on
    each other, so announcing step four when step one is filed would offer work
    nothing can claim.
  * **An announcement never fails the write it announces.** The write has landed;
    a 500 would make the client retry, and a retried `sop_create` is a second
    draft version. The cure has to stay smaller than the miss.
"""

from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from agentco import auth, events
from agentco.app import create_app

KEYS = {"dana": "dana-secret", "kofi": "kofi-secret", "operator": "op-secret"}

STEP = {
    "name": "restart-the-exporter",
    "role": "operator",
    "purpose": "get the stuck export moving",
    "definition_of_done": "The export reports done or failed, with a reason",
    "gate": {"kind": "deterministic", "check": "agentco work --status done",
             "max_park_seconds": 900, "on_timeout": "fail"},
}


@pytest.fixture()
def client(tmp_path):
    return TestClient(create_app(
        db_path=str(tmp_path / "api.sqlite3"),
        keys=KEYS,
        operator="operator",
        work_store=str(tmp_path / "work.jsonl"),
        sop_store=str(tmp_path / "sops.jsonl"),
        humans=["dana"],
    ))


def signed(method, path, actor, body=None):
    raw = json.dumps(body).encode() if body is not None else b""
    ts = str(int(time.time()))
    return {
        "X-AgentCo-Actor": actor,
        "X-AgentCo-Timestamp": ts,
        "X-AgentCo-Signature": auth.sign(KEYS[actor], method, path, ts, raw),
        "Content-Type": "application/json",
    }


def post(client, path, actor, body):
    return client.post(path, content=json.dumps(body), headers=signed("POST", path, actor, body))


def get(client, path, actor, query=""):
    return client.get(f"{path}{query}", headers=signed("GET", path, actor, None))


def feed(client, actor="kofi", since=None, kind=None):
    query = "?limit=100"
    if since is not None:
        query += f"&since={since}"
    if kind:
        query += f"&kind={kind}"
    return get(client, "/events", actor, query).json()


def kinds_on(client, kind=None):
    return [e["kind"] for e in feed(client, kind=kind)["events"]]


def only(client, kind):
    rows = [e for e in feed(client)["events"] if e["kind"] == kind]
    assert len(rows) == 1, f"expected exactly one {kind}, got {len(rows)}"
    return rows[0]


def a_sop(client, actor="dana", **over):
    body = {
        "title": "Restore a stalled export",
        "task_type": "export-repair",
        "purpose": "Get a stuck export moving without losing the partial run",
        "trigger": "An export sits in_progress past its lease",
        "roles": {"operator": {"kind": "agent"}},
        "steps": [dict(STEP)],
    }
    body.update(over)
    response = post(client, "/sops", actor, body)
    assert response.status_code == 200, response.text
    return response.json()["sop"]


# --------------------------------------------------------------------------- #
# the procedure's lifecycle reaches the feed
# --------------------------------------------------------------------------- #


def test_a_new_version_is_announced_whether_it_is_the_first_or_the_tenth(client):
    """One kind, because the subscriber's question is "is there a version I have
    not read" — and the ordinal it starts at is not a different event."""
    sop = a_sop(client)
    post(client, f"/sops/{sop['asop_id']}/activate", "dana", {"version": 1})
    post(client, f"/sops/{sop['asop_id']}/revise", "dana", {"purpose": "Get it moving, faster"})

    versioned = [e for e in feed(client)["events"] if e["kind"] == "AsopVersioned"]
    assert [e["payload"]["version"] for e in versioned] == [1, 2]
    assert {e["payload"]["asopId"] for e in versioned} == {sop["asop_id"]}


def test_activation_is_announced_because_it_changes_what_every_reader_gets(client):
    sop = a_sop(client)
    post(client, f"/sops/{sop['asop_id']}/activate", "dana", {"version": 1})

    row = only(client, "AsopActivated")
    assert row["payload"]["asopId"] == sop["asop_id"]
    assert row["payload"]["version"] == 1
    assert row["actor"] == "dana"


def test_retirement_is_announced_so_a_cached_procedure_stops_being_offered(client):
    sop = a_sop(client)
    post(client, f"/sops/{sop['asop_id']}/activate", "dana", {"version": 1})
    retired = post(client, f"/sops/{sop['asop_id']}/retire", "dana", {})
    assert retired.status_code == 200, retired.text

    assert only(client, "AsopRetired")["payload"]["asopId"] == sop["asop_id"]


# --------------------------------------------------------------------------- #
# work becoming available reaches the feed, with enough to decide on
# --------------------------------------------------------------------------- #


def test_work_filed_carries_enough_to_skip_the_pull(client):
    """`requires` and `assignedAgent` ride along on purpose: a harness that had
    to pull the queue to discover the work was not its own would be doing the
    scan this kind exists to remove."""
    response = post(client, "/work", "dana", {
        "title": "rebuild the invoice exporter",
        "requires": ["ado-write"],
        "assignedAgent": "kofi",
    })
    assert response.status_code == 200, response.text

    payload = only(client, "WorkFiled")["payload"]
    assert payload["itemId"] == response.json()["item"]["id"]
    assert payload["requires"] == ["ado-write"]
    assert payload["assignedAgent"] == "kofi"
    assert payload["blockedBy"] == []          # empty means claim it now


def test_a_run_announces_every_step_and_says_which_are_blocked(client):
    """A tree arrives blocked on itself, and the announcement says so.

    One event per filed bead, because the announcement belongs to the filing
    and `run` files each step through the same `create` every other path uses.
    What keeps that from offering unclaimable work is `blockedBy`: the step
    nothing waits on carries an empty one, and the rest name what they wait for.
    """
    sop = a_sop(client, steps=[
        dict(STEP),
        {**STEP, "name": "verify-the-export", "after": [1]},
    ])
    post(client, f"/sops/{sop['asop_id']}/activate", "dana", {"version": 1})
    run = post(client, f"/sops/{sop['asop_id']}/run", "dana", {
        "bindings": {"operator": "kofi"},
    })
    assert run.status_code == 200, run.text

    announced = [e["payload"] for e in feed(client)["events"] if e["kind"] == "WorkFiled"]
    # the run bead plus its two steps
    assert len(announced) == 3, announced
    claimable = [p for p in announced if not p["blockedBy"]]
    blocked = [p for p in announced if p["blockedBy"]]
    assert claimable and blocked, announced


# --------------------------------------------------------------------------- #
# the property the whole change exists for
# --------------------------------------------------------------------------- #


def test_a_subscriber_learns_everything_from_the_cursor_alone(client):
    """The fleet's steady state: one indexed poll per harness, and the library
    and the queue are touched only when an event says there is a reason to."""
    cursor = feed(client)["nextCursor"]

    sop = a_sop(client)
    post(client, f"/sops/{sop['asop_id']}/activate", "dana", {"version": 1})
    post(client, "/work", "dana", {"title": "rebuild the invoice exporter",
                                   "requires": ["ado-write"]})

    page = feed(client, since=cursor)
    seen = {e["kind"]: e["payload"] for e in page["events"]}
    assert {"AsopVersioned", "AsopActivated", "WorkFiled"} <= set(seen)
    assert seen["AsopActivated"]["asopId"] == sop["asop_id"]
    assert seen["WorkFiled"]["requires"] == ["ado-write"]
    assert seen["WorkFiled"]["blockedBy"] == []

    # And a quiet cycle really is quiet: nothing changed, so nothing is returned
    # and the cursor does not move. That is the poll 130 harnesses spend their
    # day on, and it has to cost nothing.
    quiet = feed(client, since=page["nextCursor"])
    assert quiet["events"] == []
    assert quiet["nextCursor"] == page["nextCursor"]


def test_a_feed_that_refuses_the_announcement_does_not_fail_the_write(monkeypatch, client):
    """The write has landed. A 500 here would make the client retry, and a
    retried `sop_create` is a second draft version."""
    def refuse(*_args, **_kwargs):
        raise RuntimeError("the feed is having a day")

    monkeypatch.setattr(events, "append", refuse)
    response = post(client, "/work", "dana", {"title": "rebuild the invoice exporter"})
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "accepted"
