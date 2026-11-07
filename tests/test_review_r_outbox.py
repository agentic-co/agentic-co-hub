"""R4 and R9 from the 2026-11-07 review (docs/known-issues.md), as properties.

Conventions are those of `tests/test_adversarial_findings.py`: each test names
the property that should hold, begins life `xfail(strict=True)`, and accepts any
honest fix. Fix the defect, delete the marker, in one change.

**R4 — the outbox's republish window.** F2 made the drainer write a receipt
before it removes the line, so the worst case after a crash is a receipt for a
line that is still queued and gets published AGAIN. "The hub dedupes on the line
id" was true for `attest` and `work_report` only. These tests model the crash
exactly: they hand the SAME outbox record (same `line_id`) to the publisher twice.
The property is the one the F2 comment states: a second publish of one line has
no second effect and returns the recorded outcome.

**R9 — claim and release are check-then-act.** Two of these are deterministic
interleavings rather than sleep races. A hook runs inside the first caller's
read-to-write window and starts the second caller on its own connection:
  * if the first caller holds no lock across the window, the second finishes
    inside it and neither sees the other — the defect;
  * if it does (a write transaction, an advisory lock), the second blocks until
    the hook gives up waiting, and the first commits before the second looks.
`_WAIT_FOR_RIVAL` is how long the hook waits to find out which of those it is;
it is a probe, not a synchronisation the test depends on to pass.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Optional

import pytest
from fastapi.testclient import TestClient

from agentco import auth, db, events, leases
from agentco.app import create_app
from agentco.outbox import Outbox, registry_publisher
from agentco.publish import Registry, RegistryError

from conftest import BACKENDS, PG_ENV_VAR  # noqa: F401  (rootdir conftest)

KEYS = {"bigmac": "bigmac-secret", "reviewer-box": "reviewer-secret", "dana": "dana-secret",
        "operator": "op-secret"}

CLAIM = {"repo": "acme/app", "prefixes": ["src/api/handlers"], "intent": "implement"}
GATE = {"kind": "deterministic", "check": "pytest -q", "max_park_seconds": 900, "on_timeout": "fail"}
STEP = {"name": "ship", "role": "implementer", "purpose": "deploy the release",
        "definition_of_done": "released", "gate": GATE}


class Loopback(Registry):
    """A real `Registry` whose transport is the real app, in process."""

    def __init__(self, actor: str, client: TestClient):
        super().__init__(actor, KEYS[actor], "http://registry.test", via="outbox")
        self.client = client

    def _call(self, method: str, path: str, body: Optional[dict] = None, query: str = "") -> dict:
        raw = json.dumps(body).encode() if body is not None else b""
        ts = str(int(time.time()))
        response = self.client.request(method, f"{path}{query}", content=raw or None, headers={
            "X-AgentCo-Actor": self.actor, "X-AgentCo-Timestamp": ts,
            "X-AgentCo-Signature": auth.sign(self.secret, method, path, ts, raw),
            "Content-Type": "application/json", "X-AgentCo-Via": "outbox",
        })
        if response.status_code >= 400:
            raise RegistryError(response.status_code, response.json())
        return response.json()


def _post(client, path, actor, body):
    raw = json.dumps(body).encode()
    ts = str(int(time.time()))
    return client.post(path, content=raw, headers={
        "X-AgentCo-Actor": actor, "X-AgentCo-Timestamp": ts,
        "X-AgentCo-Signature": auth.sign(KEYS[actor], "POST", path, ts, raw),
        "Content-Type": "application/json",
    })


@pytest.fixture()
def hub(tmp_path):
    client = TestClient(create_app(
        db_path=str(tmp_path / "api.sqlite3"), keys=KEYS, operator="operator",
        work_store=str(tmp_path / "work.jsonl"), sop_store=str(tmp_path / "sops.jsonl"),
        humans=["dana"], adjudicators=["reviewer-box"],
    ))
    client.tmp = tmp_path  # type: ignore[attr-defined]
    return client


def _kinds(client, kind: str) -> list[dict]:
    return Loopback("bigmac", client).events(kind=kind)["events"]


def _line(tmp_path, verb: str, payload: dict) -> dict:
    return Outbox(tmp_path / "box" / ".agentco").push(verb, payload, agent_label="aider")


# --------------------------------------------------------------------------- #
# R4 — one line published twice has one effect
# --------------------------------------------------------------------------- #

_R4 = "R4: the hub has no idempotency key on this verb, so a republished line acts twice"


@pytest.mark.xfail(strict=True, reason=_R4)
def test_republishing_a_claim_scope_line_opens_one_lease(hub):
    """Neighbour: `test_work_report_carries_the_line_id_as_its_idempotency_key`
    proves the KEY is passed for work_report. Nothing proves any other verb
    survives being sent twice, which is the half that matters."""
    publish = registry_publisher(Loopback("bigmac", hub))
    line = _line(hub.tmp, "claim_scope", CLAIM)
    first, second = publish(line), publish(line)
    assert first["leaseUid"] == second["leaseUid"]
    assert len(_kinds(hub, "ScopeClaimed")) == 1
    assert _kinds(hub, "ScopeConflict") == [], "a lease must not conflict with its own republish"


@pytest.mark.xfail(strict=True, reason=_R4)
def test_republishing_a_snapshot_line_records_one_snapshot(hub):
    publish = registry_publisher(Loopback("bigmac", hub))
    line = _line(hub.tmp, "snapshot", {"artifactUri": "git:/r#main", "purpose": "baseline"})
    first, second = publish(line), publish(line)
    assert first["snapId"] == second["snapId"]
    assert len(_kinds(hub, "SnapshotTaken")) == 1


def _executed(client) -> dict:
    item = _post(client, "/work", "operator", {"title": "migrate"}).json()["item"]
    pulled = _post(client, "/work/pull", "bigmac", {}).json()
    assert pulled["state"] == "leased"
    done = _post(client, f"/work/{item['id']}/report", "bigmac",
                 {"attempt": pulled["attempt"], "status": "done"})
    assert done.status_code == 200, done.text
    return item


@pytest.mark.xfail(strict=True, reason=_R4)
def test_republishing_an_adjudicate_line_returns_the_recorded_tag_not_a_refusal(hub):
    """An adjudication is immutable, so the second send is refused
    `adjudication_exists` — the drainer then records a REFUSAL receipt for a line
    whose first send succeeded. The recorded outcome is the right answer."""
    item = _executed(hub)
    publish = registry_publisher(Loopback("reviewer-box", hub))
    line = _line(hub.tmp, "adjudicate", {"itemId": item["id"], "verdict": "bad",
                                         "evidence": "the transcript shows the check was never run"})
    first = publish(line)
    second = publish(line)  # must not raise
    assert second["item"]["metadata"]["adjudication"] == first["item"]["metadata"]["adjudication"]


def _a_procedure(client) -> str:
    from agentco.policy import HUMAN
    from agentco.sop import SopLibrary

    library = SopLibrary(client.tmp / "sops.jsonl")
    sop = library.create("deploy", author="dana", author_kind=HUMAN,
                         roles={"implementer": {"kind": "agent"}}, steps=[STEP])
    library.activate(sop.asop_id, 1, author="dana", author_kind=HUMAN)
    return sop.asop_id


@pytest.mark.xfail(strict=True, reason=_R4)
def test_republishing_a_sop_revise_line_writes_one_version(hub):
    from agentco.sop import SopLibrary

    sop_id = _a_procedure(hub)
    publish = registry_publisher(Loopback("bigmac", hub))
    line = _line(hub.tmp, "sop_revise", {"sopId": sop_id, "changes": {"purpose": "ship it carefully"}})
    first, second = publish(line), publish(line)
    assert first["sop"]["version"] == second["sop"]["version"]
    assert [a.version for a in SopLibrary(hub.tmp / "sops.jsonl").history(sop_id)] == [1, 2]


@pytest.mark.xfail(strict=True, reason=_R4)
def test_republishing_a_sop_activate_line_announces_one_activation(hub):
    from agentco.sop import SopLibrary

    sop_id = _a_procedure(hub)
    SopLibrary(hub.tmp / "sops.jsonl").revise(sop_id, author="dana", author_kind="human",
                                              purpose="v2")
    publish = registry_publisher(Loopback("dana", hub))
    line = _line(hub.tmp, "sop_activate", {"sopId": sop_id, "version": 2})
    first, second = publish(line), publish(line)
    assert first["sop"]["version"] == second["sop"]["version"] == 2
    activations = [e for e in _kinds(hub, "AsopActivated") if e["payload"].get("version") == 2]
    assert len(activations) == 1


def test_a_second_release_is_a_clean_outcome_for_the_drainer(hub):
    """`release_scope` is idempotent by state and has to stay so: the drainer
    must never record a refusal for the second send of a release."""
    registry = Loopback("bigmac", hub)
    lease = registry.claim_scope(**{"repo": CLAIM["repo"], "prefixes": CLAIM["prefixes"],
                                    "intent": CLAIM["intent"]})
    publish = registry_publisher(registry)
    line = _line(hub.tmp, "release_scope", {"leaseUid": lease["leaseUid"]})
    publish(line)
    again = publish(line)  # must not raise
    assert again.get("state") in {"duplicate", "accepted"}
    assert len(_kinds(hub, "ScopeReleased")) == 1


def test_one_actors_key_never_replays_another_actors_outcome(hub):
    """A GUARD, passing today: whatever shape the fix takes, the key is scoped
    to the actor. Otherwise one machine guessing a line id reads, or blocks,
    another's claim."""
    line = _line(hub.tmp, "claim_scope", CLAIM)
    mine = registry_publisher(Loopback("bigmac", hub))(line)
    theirs = registry_publisher(Loopback("reviewer-box", hub))({**line, "payload": {
        **CLAIM, "prefixes": ["src/other/place"]}})
    assert mine["leaseUid"] != theirs["leaseUid"]
    assert theirs["prefixes"] == ["src/other/place"]


# --------------------------------------------------------------------------- #
# R9 — claim and release are one transaction each
# --------------------------------------------------------------------------- #

_R9 = "R9: leases.claim/release are check-then-act across separate commits"
_WAIT_FOR_RIVAL = 1.0

_R9_BACKENDS = [b for b in BACKENDS if b != "jsonl"]


@pytest.fixture(params=_R9_BACKENDS)
def target(request, tmp_path):
    if request.param == "postgres":
        return request.getfixturevalue("_pg_dsn")
    return tmp_path / "r9.sqlite3"


class Hooked:
    """A connection that runs `hook` after the first statement matching `needle`.

    Delegates everything else. Not a mock of the database: the statements still
    run on the real one, the hook only chooses the moment another caller starts.
    """

    def __init__(self, real, needle: str, hook):
        self._real, self._needle, self._hook, self._fired = real, needle, hook, False

    def execute(self, sql, *args, **kwargs):
        out = self._real.execute(sql, *args, **kwargs)
        if not self._fired and self._needle in " ".join(str(sql).split()):
            self._fired = True
            self._hook()
        return out

    def __enter__(self):
        self._real.__enter__()
        return self

    def __exit__(self, *exc):
        return self._real.__exit__(*exc)

    def __getattr__(self, name):
        return getattr(self._real, name)


def _rival(fn):
    """Start `fn` on its own thread; wait a bounded time to see whether it can finish."""
    box: dict = {}

    def run():
        try:
            box["out"] = fn()
        except BaseException as exc:  # noqa: BLE001 - surfaced by the test
            box["err"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(_WAIT_FOR_RIVAL)
    return thread, box


@pytest.mark.xfail(strict=True, reason=_R9)
def test_two_overlapping_claims_cannot_both_miss_each_other(target):
    """Two processes claiming overlapping scope: at least one of them must be told.

    Neighbour: `tests/test_leases.py` claims sequentially, which is the case the
    check-then-act code gets right. The first caller's hook starts the second
    inside its read-to-insert window."""
    first_conn, second_conn = db.connect(target), db.connect(target)
    outcome: dict = {}

    def second_claims():
        return leases.claim(second_conn, actor="kofi", repo="acme/app",
                            prefixes=["src/api/handlers"], intent="implement")

    state: dict = {}

    def hook():
        state["thread"], state["box"] = _rival(second_claims)

    hooked = Hooked(first_conn, "FROM leases", hook)
    outcome["first"] = leases.claim(hooked, actor="dana", repo="acme/app",
                                    prefixes=["src/api/handlers"], intent="implement")
    state["thread"].join(30)
    assert "err" not in state["box"], state["box"].get("err")
    outcome["second"] = state["box"]["out"]
    assert outcome["first"]["conflicts"] or outcome["second"]["conflicts"], (
        "both claimants were told nobody else was there"
    )


@pytest.mark.xfail(strict=True, reason=_R9)
def test_a_claim_whose_event_cannot_be_written_leaves_no_lease(target, monkeypatch):
    """A crash between the lease commit and the event commit leaves a lease no
    feed subscriber ever saw. Either both land or neither does."""
    conn = db.connect(target)

    def crash(*a, **k):
        raise RuntimeError("process died before the event landed")

    monkeypatch.setattr(events, "append", crash)
    monkeypatch.setattr(events, "insert", crash)
    with pytest.raises(RuntimeError):
        leases.claim(conn, actor="dana", repo="acme/app", prefixes=["src/api/handlers"], intent="implement")
    assert leases.live_leases(conn, "acme/app") == []


@pytest.mark.xfail(strict=True, reason=_R9)
def test_two_concurrent_releases_announce_one_release(target):
    """`release`'s UPDATE has no `released_at IS NULL` guard, so two releasers
    that both read an open lease both write and both emit."""
    first_conn, second_conn = db.connect(target), db.connect(target)
    lease = leases.claim(first_conn, actor="dana", repo="acme/app",
                         prefixes=["src/api/handlers"], intent="implement")
    uid = lease["leaseUid"]
    state: dict = {}

    def hook():
        state["thread"], state["box"] = _rival(
            lambda: leases.release(second_conn, actor="dana", lease_uid=uid))

    hooked = Hooked(first_conn, "FROM leases WHERE uid", hook)
    leases.release(hooked, actor="dana", lease_uid=uid)
    state["thread"].join(30)
    assert "err" not in state["box"], state["box"].get("err")
    released = list(first_conn.execute("SELECT kind FROM events WHERE kind = 'ScopeReleased'"))
    assert len(released) == 1
