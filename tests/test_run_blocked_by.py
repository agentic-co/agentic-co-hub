"""A run's `blockedBy` holds its ROOT steps (owner decision 2026-10-18, D7 option A).

Found by the live wire probes: `POST /sops/{id}/run` dropped `blockedBy` with a
200, and even in the store a run's `blocked_by` sat on the parent only, so its
steps were pullable while the run read as blocked.

Now:
  * every step with no in-run predecessor waits on the run's blockers, and so
    do a `uses` step's inner roots (they inherit what the outer step waits on);
  * a root unblocks when every blocker is DONE; a FAILED blocker keeps it
    blocked, and the run view says what it is waiting on (`waitingOn`);
  * an unknown, duplicated or malformed blocker is refused 422 before anything
    is filed;
  * the route forwards every keyword `run` accepts, pinned by a signature test.
"""

from __future__ import annotations

import inspect
import json
import time

import pytest

from agentco.app import RUN_BODY_FIELDS, RUN_NOT_ON_THE_WIRE
from agentco.errors import Refusal
from agentco.sop import SopLibrary
from agentco.work import Queue, WorkStatus

GATE = {"kind": "deterministic", "check": "make check", "max_park_seconds": 900, "on_timeout": "fail"}
ATT = {"check": "make check", "exit_status": 0, "environment": "ci/synthetic", "at": "2026-10-18T00:00:00+00:00"}


def three_steps(**over):
    """Step 1 and step 3 are roots; step 2 is after step 1."""
    return {"inputs": [], "roles": {"w": {"kind": "agent"}}, "steps": [
        {"name": "one", "role": "w", "purpose": "one", "gate": GATE},
        {"name": "two", "role": "w", "purpose": "two", "gate": GATE, "after": [1]},
        # `after: []` makes it a root: an omitted `after` means "after the step before".
        {"name": "three", "role": "w", "purpose": "three", "gate": GATE, "after": []},
    ], **over}


def active(library, title, body):
    asop = library.create(title, **body)
    return library.activate(asop.asop_id, 1, author_kind="human")


def ready_ids(queue):
    return {i.id for i in queue.ready()}


def finish(queue, item_id, status=WorkStatus.DONE):
    claimed = queue.claim(item_id, "agent-a")
    return queue.report_result(item_id, claimed.lease_attempt, status,
                               attestation=ATT if status is WorkStatus.DONE and queue.get(item_id).verify else None)


# --------------------------------------------------------------------------- #
# the regression check for the class
# --------------------------------------------------------------------------- #


def test_the_route_forwards_every_keyword_run_accepts():
    accepted = (set(inspect.signature(SopLibrary.run).parameters) | set(inspect.signature(Queue.create).parameters))
    accepted -= {"self", "work_kwargs"}
    forwarded = set(RUN_BODY_FIELDS.values())
    assert forwarded | set(RUN_NOT_ON_THE_WIRE) == accepted, (
        f"not forwarded and not excused: {sorted(accepted - forwarded - set(RUN_NOT_ON_THE_WIRE))}; "
        f"forwarded but not accepted: {sorted(forwarded - accepted)}")
    assert not forwarded & set(RUN_NOT_ON_THE_WIRE)


# --------------------------------------------------------------------------- #
# the store, on every backend
# --------------------------------------------------------------------------- #


def test_roots_wait_on_the_upstream_and_release_when_it_is_done(library, queue):
    asop = active(library, "three steps", three_steps())
    upstream = queue.create("upstream")
    run = library.run(asop.asop_id, queue, bindings={"w": "agent-a"}, blocked_by=[upstream.id])
    one, two, three = (s["itemId"] for s in run["steps"])
    assert run["blockedBy"] == [upstream.id]
    assert queue.get(one).blocked_by == [upstream.id] and queue.get(three).blocked_by == [upstream.id]
    assert queue.get(two).blocked_by == [one], "a non-root step still waits only on its predecessors"
    assert upstream.id not in queue.get(run["runId"]).blocked_by, "the parent waits on its steps"
    assert not {one, two, three} & ready_ids(queue)
    view = library.run_get(run["runId"], queue)
    assert view["waitingOn"] == [{"itemId": upstream.id, "status": "pending"}]

    finish(queue, upstream.id)
    assert {one, three} <= ready_ids(queue) and two not in ready_ids(queue)
    assert library.run_get(run["runId"], queue)["waitingOn"] == []


def test_a_failed_upstream_keeps_the_roots_blocked_and_says_why(library, queue):
    asop = active(library, "three steps", three_steps())
    upstream = queue.create("upstream")
    run = library.run(asop.asop_id, queue, bindings={"w": "agent-a"}, blocked_by=[upstream.id])
    finish(queue, upstream.id, WorkStatus.FAILED)
    assert not {s["itemId"] for s in run["steps"]} & ready_ids(queue), "never silently released"
    assert library.run_get(run["runId"], queue)["waitingOn"] == [{"itemId": upstream.id, "status": "failed"}]


def test_every_blocker_must_be_done(library, queue):
    asop = active(library, "three steps", three_steps())
    a, b = queue.create("a"), queue.create("b")
    run = library.run(asop.asop_id, queue, bindings={"w": "agent-a"}, blocked_by=[a.id, b.id])
    finish(queue, a.id)
    assert run["steps"][0]["itemId"] not in ready_ids(queue)
    finish(queue, b.id)
    assert run["steps"][0]["itemId"] in ready_ids(queue)


def test_nested_inner_roots_inherit_what_the_outer_step_waits_on(library, queue):
    inner = active(library, "inner", {"inputs": [], "roles": {"w": {"kind": "agent"}}, "steps": [
        {"name": "in-one", "role": "w", "purpose": "a", "gate": GATE},
        {"name": "in-two", "role": "w", "purpose": "b", "gate": GATE, "after": [1]}]})
    outer = active(library, "outer", {"inputs": [], "roles": {"w": {"kind": "agent"}}, "steps": [
        {"name": "uses-inner-first", "uses": {"asop_id": inner.asop_id, "version": 1}},
        {"name": "prep", "role": "w", "purpose": "prep", "gate": GATE, "after": []},
        {"name": "uses-inner-after", "uses": {"asop_id": inner.asop_id, "version": 1}, "after": [2]}]})
    upstream = queue.create("upstream")
    run = library.run(outer.asop_id, queue, bindings={"w": "agent-a"}, blocked_by=[upstream.id])
    root_uses, prep, later_uses = run["steps"]
    in_root = root_uses["children"][0]["itemId"]
    later_root = later_uses["children"][0]["itemId"]
    assert queue.get(in_root).blocked_by == [upstream.id], "a root `uses` step's inner root waits on the run"
    assert queue.get(later_root).blocked_by == [prep["itemId"]], "and an inner root waits on its outer step's predecessors"
    assert not {in_root, later_root, prep["itemId"]} & ready_ids(queue)
    finish(queue, upstream.id)
    assert {in_root, prep["itemId"]} <= ready_ids(queue) and later_root not in ready_ids(queue)
    finish(queue, prep["itemId"])
    assert later_root in ready_ids(queue)


@pytest.mark.parametrize("blocked_by", [["w-does-not-exist"], "w-a-string", [""], None])
def test_a_bad_blocker_is_refused_before_anything_is_filed(library, queue, blocked_by):
    asop = active(library, "three steps", three_steps())
    before = len(queue.list())
    if blocked_by is None:  # the duplicate case needs a real id
        real = queue.create("real")
        before = len(queue.list())
        blocked_by = [real.id, real.id]
    with pytest.raises(Refusal) as exc:
        library.run(asop.asop_id, queue, bindings={"w": "agent-a"}, blocked_by=blocked_by)
    assert exc.value.code == "blocked_by_invalid" and exc.value.http_status == 422
    assert len(queue.list()) == before, "a refused run files nothing"


def test_no_blockers_is_exactly_the_old_behaviour(library, queue):
    asop = active(library, "three steps", three_steps())
    run = library.run(asop.asop_id, queue, bindings={"w": "agent-a"})
    assert run["blockedBy"] == []
    assert {run["steps"][0]["itemId"], run["steps"][2]["itemId"]} <= ready_ids(queue)


# --------------------------------------------------------------------------- #
# over the real route, and over a real socket
# --------------------------------------------------------------------------- #

from tests.test_mcp_remote import KEYS, LoopbackRegistry, client  # noqa: E402,F401


def http_asop(reg):
    sop = reg._call("POST", "/sops", {"title": "three steps", **three_steps()})["sop"]
    reg._call("POST", f"/sops/{sop['asop_id']}/activate", {"version": 1})
    return sop["asop_id"]


def test_http_forwards_blocked_by_and_refuses_an_unknown_one(client):
    reg = LoopbackRegistry("macbook", client)
    asop_id = http_asop(reg)
    upstream = reg.work_create("upstream")["item"]
    run = reg.sop_run(asop_id, {}, {"w": "macbook"}, blockedBy=[upstream["id"]])["run"]
    assert run["blockedBy"] == [upstream["id"]]
    assert reg.work_pull().get("item", {}).get("id") == upstream["id"], "only the upstream is offered"

    before = len(reg._call("GET", "/work")["items"])
    from agentco.publish import RegistryError
    with pytest.raises(RegistryError) as exc:
        reg.sop_run(asop_id, {}, {"w": "macbook"}, blockedBy=["w-nope"])
    assert exc.value.status == 422 and exc.value.payload["code"] == "blocked_by_invalid"
    assert len(reg._call("GET", "/work")["items"]) == before


@pytest.fixture()
def live_hub(tmp_path):
    import socket
    import threading

    import uvicorn

    from agentco.app import create_app

    app = create_app(db_path=str(tmp_path / "live.sqlite3"), keys=KEYS, operator="operator",
                     work_store=str(tmp_path / "work.jsonl"), sop_store=str(tmp_path / "sops.jsonl"))
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)


def test_live_wire_roots_wait_then_release(live_hub):
    from agentco.publish import Registry

    reg = Registry("macbook", KEYS["macbook"], live_hub)
    asop_id = http_asop(reg)
    upstream = reg.work_create("upstream")["item"]
    run = reg.sop_run(asop_id, {}, {"w": "macbook"}, blockedBy=[upstream["id"]])["run"]
    roots = {run["steps"][0]["itemId"], run["steps"][2]["itemId"]}
    pulled = reg.work_pull()
    assert pulled["item"]["id"] == upstream["id"]
    reg.work_report(upstream["id"], pulled["attempt"], "done")
    view = reg._call("GET", f"/runs/{run['runId']}")["run"]
    assert view["waitingOn"] == [] and view["blockedBy"] == [upstream["id"]]
    assert reg.work_pull()["item"]["id"] in roots
    assert json.dumps(view).count(upstream["id"]) >= 1
