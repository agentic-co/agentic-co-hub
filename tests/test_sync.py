"""The sync client: the half that collects on the feed carrying ASOP lifecycle.

Two levels. Most of it runs against a fake plane, because the questions are
about the client's own rules — which kinds it acts on, what it does when a
fetch fails, and when the cursor is allowed to move. One test runs against the
REAL app over a signed transport, because a client that only ever sees a mock's
idea of a response is a client nobody has checked against the thing it talks to.
"""

from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from agentco import auth
from agentco.app import create_app
from agentco.sync import AsopCache, Sync

KEYS = {"dana": "dana-secret", "kofi": "kofi-secret", "operator": "op-secret"}

STEP = {
    "name": "do-the-thing",
    "role": "worker",
    "purpose": "the work itself",
    "definition_of_done": "it is done",
    "gate": {"kind": "deterministic", "check": "true",
             "max_park_seconds": 900, "on_timeout": "fail"},
}


class FakePlane:
    """Answers `events` and `sop_get`, and counts what it was asked.

    The count is the point of several tests below: this client exists so that
    the expensive read happens only when an event says it should, and "it did
    not ask" is the assertion.
    """

    def __init__(self, pages, records=None, fail_on=()):
        self.pages = list(pages)
        self.records = records or {}
        self.fail_on = set(fail_on)
        self.fetches = []
        self.since_seen = []

    def events(self, since=None, limit=200):
        self.since_seen.append(since)
        return self.pages.pop(0) if self.pages else {"events": [], "nextCursor": since}

    def sop_get(self, asop_id, version=None):
        self.fetches.append(asop_id)
        if asop_id in self.fail_on:
            raise RuntimeError("the plane refused")
        return {"sop": self.records.get(asop_id)}


def page(*events, cursor="c1"):
    return {"events": list(events), "nextCursor": cursor}


def event(kind, asop_id, **payload):
    return {"kind": kind, "actor": "dana", "payload": {"asopId": asop_id, **payload}}


def test_a_quiet_pass_changes_nothing_and_asks_for_nothing(tmp_path):
    plane = FakePlane([page(cursor="c0")])
    report = Sync(plane, AsopCache(tmp_path)).once()

    assert report.events_seen == 0
    assert not report.changed
    assert plane.fetches == [], "it read the library with no reason to"
    assert report.summary() == "nothing new"


def test_an_activation_fetches_that_one_procedure_and_caches_it(tmp_path):
    record = {"asop_id": "asop-1", "version": 2, "title": "Ship a change"}
    plane = FakePlane([page(event("AsopActivated", "asop-1", version=2))],
                      records={"asop-1": record})
    cache = AsopCache(tmp_path)

    report = Sync(plane, cache).once()

    assert report.activated == ["asop-1"]
    assert plane.fetches == ["asop-1"], "it fetched something other than the one announced"
    assert cache.get("asop-1") == record
    assert cache.held() == ["asop-1"]


def test_a_retirement_drops_it_without_asking_the_plane(tmp_path):
    cache = AsopCache(tmp_path)
    cache.put("asop-1", {"asop_id": "asop-1", "version": 1})
    plane = FakePlane([page(event("AsopRetired", "asop-1"))])

    report = Sync(plane, cache).once()

    assert report.retired == ["asop-1"]
    assert cache.held() == []
    assert plane.fetches == [], "a retirement needs no read — the event says everything"


def test_kinds_it_does_not_understand_are_counted_and_left_alone(tmp_path):
    """The vocabulary is meant to grow. A client that acted on kinds it did not
    know would break on the next one added."""
    plane = FakePlane([page(
        {"kind": "ScopeClaimed", "actor": "kofi", "payload": {"repo": "acme/web"}},
        {"kind": "DigestReceived", "actor": "child", "payload": {"text": "a rollup"}},
        {"kind": "SomethingInventedNextYear", "actor": "x", "payload": {}},
    )])
    report = Sync(plane, AsopCache(tmp_path)).once()

    assert report.events_seen == 3
    assert not report.changed
    assert plane.fetches == []


def test_the_cursor_resumes_from_where_the_last_pass_stopped(tmp_path):
    cache = AsopCache(tmp_path)
    plane = FakePlane([page(cursor="c1"), page(cursor="c2")])

    Sync(plane, cache).once()
    assert cache.cursor() == "c1"
    Sync(plane, cache).once()

    assert plane.since_seen == [None, "c1"], "it did not resume from the saved position"
    assert cache.cursor() == "c2"


def test_a_failed_pass_leaves_the_cursor_where_it_was(tmp_path):
    """Otherwise a transport blip silently skips every event in that page, and
    the harness looks up to date precisely because it missed them."""
    cache = AsopCache(tmp_path)
    cache.save_cursor("c1")

    class Broken(FakePlane):
        def events(self, since=None, limit=200):
            raise RuntimeError("the network went away")

    with pytest.raises(RuntimeError):
        Sync(Broken([]), cache).once()

    assert cache.cursor() == "c1"


def test_one_unreadable_procedure_does_not_strand_every_later_event(tmp_path):
    """The opposite failure: a single bad record must not block the cursor
    forever. It is named in the report rather than left to be inferred."""
    good = {"asop_id": "asop-2", "version": 1}
    plane = FakePlane(
        [page(event("AsopActivated", "asop-1"), event("AsopActivated", "asop-2"), cursor="c9")],
        records={"asop-2": good},
        fail_on={"asop-1"},
    )
    cache = AsopCache(tmp_path)

    report = Sync(plane, cache).once()

    assert report.unreadable == ["asop-1"]
    assert report.activated == ["asop-2"]
    assert cache.cursor() == "c9"
    assert "unreadable" in report.summary()


# --------------------------------------------------------------------------- #
# against the real plane
# --------------------------------------------------------------------------- #


@pytest.fixture()
def live(tmp_path):
    app = create_app(
        db_path=str(tmp_path / "api.sqlite3"),
        keys=KEYS,
        operator="operator",
        work_store=str(tmp_path / "work.jsonl"),
        sop_store=str(tmp_path / "sops.jsonl"),
        humans=["dana"],
    )
    return TestClient(app)


class SignedTransport:
    """`Registry`'s two methods, over the test client, signed for real."""

    def __init__(self, client, actor="kofi"):
        self.client = client
        self.actor = actor

    def _headers(self, method, path, body=None):
        raw = json.dumps(body).encode() if body is not None else b""
        ts = str(int(time.time()))
        return {"X-AgentCo-Actor": self.actor, "X-AgentCo-Timestamp": ts,
                "X-AgentCo-Signature": auth.sign(KEYS[self.actor], method, path, ts, raw),
                "Content-Type": "application/json"}

    def events(self, since=None, limit=200):
        query = f"?limit={limit}" + (f"&since={since}" if since else "")
        r = self.client.get(f"/events{query}", headers=self._headers("GET", "/events"))
        assert r.status_code == 200, r.text
        return r.json()

    def sop_get(self, asop_id, version=None):
        path = f"/sops/{asop_id}"
        r = self.client.get(path, headers=self._headers("GET", path))
        assert r.status_code == 200, r.text
        return r.json()


def post(client, path, actor, body):
    raw = json.dumps(body)
    ts = str(int(time.time()))
    headers = {"X-AgentCo-Actor": actor, "X-AgentCo-Timestamp": ts,
               "X-AgentCo-Signature": auth.sign(KEYS[actor], "POST", path, ts, raw.encode()),
               "Content-Type": "application/json"}
    return client.post(path, content=raw, headers=headers)


def test_the_client_tracks_the_real_plane_through_a_procedure_lifecycle(live, tmp_path):
    """Authored, activated, revised, retired — and the cache follows, with the
    harness never listing the library once."""
    plane = SignedTransport(live)
    cache = AsopCache(tmp_path / "harness")
    sync = Sync(plane, cache)

    assert sync.once().changed is False
    assert cache.held() == []

    body = {"title": "Ship a change", "task_type": "change",
            "purpose": "get a change out safely", "trigger": "a change is ready",
            "roles": {"worker": {"kind": "agent"}}, "steps": [dict(STEP)]}
    sop = post(live, "/sops", "dana", body).json()["sop"]
    asop_id = sop["asop_id"]

    # A draft alone puts nothing in the cache: the plane serves the ACTIVE
    # version, and a harness must not execute something nobody activated.
    report = sync.once()
    assert cache.held() == [], f"a draft reached the cache: {report.summary()}"

    assert post(live, f"/sops/{asop_id}/activate", "dana", {"version": 1}).status_code == 200
    report = sync.once()
    assert report.activated == [asop_id]
    assert cache.get(asop_id)["version"] == 1

    post(live, f"/sops/{asop_id}/revise", "dana", {"purpose": "get a change out safely, faster"})
    assert post(live, f"/sops/{asop_id}/activate", "dana", {"version": 2}).status_code == 200
    sync.once()
    assert cache.get(asop_id)["version"] == 2, "the harness is holding a superseded version"

    assert post(live, f"/sops/{asop_id}/retire", "dana", {}).status_code == 200
    report = sync.once()
    assert report.retired == [asop_id]
    assert cache.held() == [], "a withdrawn procedure is still on disk"
