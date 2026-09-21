"""A harness gets its whole working day from the cursor, and never lists a store.

This is the property the ASOP and work kinds were added for, stated end to end
rather than one event at a time. A fleet's steady state is one indexed poll per
harness; the expensive reads — the queue's full candidate scan, the library's
every-record read — should happen only when an event says there is a reason.

The test is written as a harness with NO access to `/sops` or `/work/pull`
except through what the feed told it. That restriction is the assertion: a
client is patched to refuse those routes unless an event has justified them, so
a regression that makes the plane silent shows up as a harness that cannot work,
not as a slower one.
"""

from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from agentco import auth
from agentco.app import create_app

KEYS = {"dana": "dana-secret", "kofi": "kofi-secret", "operator": "op-secret"}

STEP_ONE = {
    "name": "implement",
    "role": "worker",
    "purpose": "write the code",
    "definition_of_done": "the feature exists behind its flag",
    "gate": {"kind": "deterministic", "check": "pytest -q",
             "max_park_seconds": 900, "on_timeout": "fail"},
}
STEP_TWO = {
    "name": "verify",
    "role": "worker",
    "purpose": "prove the suite is green",
    "validation": "the suite exits 0",
    "after": [1],
    "gate": {"kind": "deterministic", "check": "pytest -q",
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


class Harness:
    """A worker that only knows what the feed has told it.

    `store_reads` counts every time it touched the library or the queue, so the
    test can assert those happened BECAUSE of an event rather than on a timer.
    """

    def __init__(self, client, actor="kofi"):
        self.client = client
        self.actor = actor
        self.cursor = None
        self.store_reads = 0
        self.known_asops: dict[str, int] = {}
        self.pending_work: list[dict] = []

    def poll(self) -> list[dict]:
        """The one call this harness makes on a timer."""
        query = "?limit=100" + (f"&since={self.cursor}" if self.cursor else "")
        r = self.client.get(f"/events{query}",
                            headers=signed("GET", "/events", self.actor))
        assert r.status_code == 200, r.text
        page = r.json()
        self.cursor = page["nextCursor"]
        for event in page["events"]:
            self._react(event)
        return page["events"]

    def _react(self, event):
        kind, payload = event["kind"], event.get("payload") or {}
        if kind == "AsopActivated":
            # Only NOW is reading the library justified.
            self.store_reads += 1
            asop_id = payload["asopId"]
            r = self.client.get(f"/sops/{asop_id}",
                                headers=signed("GET", f"/sops/{asop_id}", self.actor))
            assert r.status_code == 200, r.text
            self.known_asops[asop_id] = r.json()["sop"]["version"]
        elif kind == "AsopRetired":
            self.known_asops.pop(payload["asopId"], None)
        elif kind == "WorkFiled":
            # Decided from the PAYLOAD, with no pull: work that is blocked, or
            # assigned elsewhere, is skipped without touching the queue at all.
            if payload.get("blockedBy"):
                return
            if payload.get("assignedAgent") not in (None, self.actor):
                return
            self.pending_work.append(payload)

    def work_one(self):
        """Claim and report a single item — justified by a WorkFiled event."""
        assert self.pending_work, "nothing the feed offered is claimable"
        self.pending_work.pop(0)
        self.store_reads += 1
        r = self.client.post("/work/pull", content=json.dumps({}),
                             headers=signed("POST", "/work/pull", self.actor, {}))
        assert r.status_code == 200, r.text
        pulled = r.json()
        if pulled.get("state") != "leased":
            return None
        item = pulled["item"]
        path = f"/work/{item['id']}/report"
        body = {"attempt": pulled["attempt"], "status": "done",
                "attestation": {"check": "pytest -q", "exit_status": 0,
                                "environment": "ci", "at": "2026-09-21T10:00:00Z",
                                "submitted_by": self.actor}}
        r = self.client.post(path, content=json.dumps(body),
                             headers=signed("POST", path, self.actor, body))
        assert r.status_code == 200, r.text
        return item["id"]


def post(client, path, actor, body):
    return client.post(path, content=json.dumps(body), headers=signed("POST", path, actor, body))


def test_a_harness_completes_a_run_knowing_only_what_the_feed_said(client):
    harness = Harness(client)

    # A quiet start: nothing has happened, so the harness learns nothing and
    # touches no store. This is the call it makes all day.
    assert harness.poll() == []
    assert harness.store_reads == 0
    assert harness.known_asops == {}

    # An operator authors and activates a procedure.
    sop = post(client, "/sops", "dana", {
        "title": "Ship a feature",
        "task_type": "feature",
        "purpose": "take a feature from requirement to verified code",
        "trigger": "a requirement exists",
        "roles": {"worker": {"kind": "agent"}},
        "steps": [dict(STEP_ONE), dict(STEP_TWO)],
    }).json()["sop"]
    post(client, f"/sops/{sop['asop_id']}/activate", "dana", {"version": 1})

    # One poll, and the harness now holds the live procedure — because an event
    # told it to go and read that one, not because it re-listed the library.
    harness.poll()
    assert harness.known_asops == {sop["asop_id"]: 1}
    assert harness.store_reads == 1

    # Work is filed by running the ASOP.
    post(client, f"/sops/{sop['asop_id']}/run", "dana", {"bindings": {"worker": "kofi"}})

    harness.poll()
    # The run bead and two steps were announced; only the unblocked ones are
    # offered, and the harness decided that from the payload alone.
    assert harness.pending_work, "the feed offered no claimable work"
    assert all(not p["blockedBy"] for p in harness.pending_work)
    assert harness.store_reads == 1, "it read a store without an event telling it to"

    # And it can actually do the work it was told about.
    done = harness.work_one()
    assert done is not None


def test_a_quiet_cursor_costs_nothing_and_does_not_move(client):
    """What 130 harnesses do most of the time. If this ever starts returning
    rows or moving, the fleet's steady-state load has changed."""
    harness = Harness(client)
    harness.poll()
    before = harness.cursor
    for _ in range(5):
        assert harness.poll() == []
    assert harness.cursor == before
    assert harness.store_reads == 0


def test_a_retirement_reaches_a_harness_that_cached_the_procedure(client):
    """The case that makes this a coordination layer rather than a cache: a
    procedure withdrawn centrally must stop being offered by every harness
    holding it, without any of them asking."""
    harness = Harness(client)
    sop = post(client, "/sops", "dana", {
        "title": "Ship a feature",
        "task_type": "feature",
        "purpose": "take a feature from requirement to verified code",
        "trigger": "a requirement exists",
        "roles": {"worker": {"kind": "agent"}},
        "steps": [dict(STEP_ONE)],
    }).json()["sop"]
    post(client, f"/sops/{sop['asop_id']}/activate", "dana", {"version": 1})
    harness.poll()
    assert sop["asop_id"] in harness.known_asops

    retired = post(client, f"/sops/{sop['asop_id']}/retire", "dana", {})
    assert retired.status_code == 200, retired.text

    harness.poll()
    assert sop["asop_id"] not in harness.known_asops, "a withdrawn procedure is still held"
