"""Lease renew: the heartbeat a long step sends (owner decision 10, 2026-10-18).

A renewal extends a held lease for its current fence holder only. It never moves
the fence or touches the repair budget, emits nothing on the feed, and stops at a
maximum total lease age measured from the claim that took the lease.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from agentco.errors import Refusal
from agentco.work import LeaseError, WorkStatus, lease_max_age_from_env, lease_started_at

GATE = {"kind": "deterministic", "check": "make check", "max_park_seconds": 900, "on_timeout": "fail"}
ATT = {"check": "make check", "exit_status": 0, "environment": "ci/synthetic", "at": "2026-10-18T00:00:00+00:00"}
T0 = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)


def held(queue, agent="worker-a", now=T0, ttl=60):
    item = queue.create("long step", verify=GATE)
    return queue.claim(item.id, agent, ttl_seconds=ttl, now=now)


def test_a_renewal_extends_only_the_expiry(queue):
    claimed = held(queue)
    renewed = queue.renew(claimed.id, claimed.lease_attempt, "worker-a", ttl_seconds=600,
                          now=T0 + timedelta(seconds=30))
    assert renewed.lease_attempt == claimed.lease_attempt, "the fence does not move"
    assert renewed.verify_failures == claimed.verify_failures == 0
    assert renewed.status is WorkStatus.IN_PROGRESS and renewed.leased_by == "worker-a"
    assert datetime.fromisoformat(renewed.lease_expires_at) == T0 + timedelta(seconds=630)
    assert renewed.metadata["lease_renewed"]["count"] == 1
    assert lease_started_at(renewed) == T0, "the lease's age still counts from its claim"
    again = queue.renew(claimed.id, claimed.lease_attempt, "worker-a", ttl_seconds=600,
                        now=T0 + timedelta(seconds=60))
    assert again.metadata["lease_renewed"]["count"] == 2
    done = queue.report_result(claimed.id, claimed.lease_attempt, WorkStatus.DONE, attestation=ATT,
                               now=T0 + timedelta(seconds=90))
    assert done.status is WorkStatus.DONE, "the holder reports on the same attempt it renewed"


def test_only_the_holder_at_the_current_fence_renews(queue):
    claimed = held(queue)
    with pytest.raises(Refusal) as exc:
        queue.renew(claimed.id, claimed.lease_attempt, "worker-b", now=T0 + timedelta(seconds=10))
    assert exc.value.code == "not_the_holder"
    with pytest.raises(LeaseError, match="superseded"):
        queue.renew(claimed.id, claimed.lease_attempt - 1, "worker-a", now=T0 + timedelta(seconds=10))
    with pytest.raises(LeaseError, match="expired"):
        queue.renew(claimed.id, claimed.lease_attempt, "worker-a", now=T0 + timedelta(seconds=61))
    retaken = queue.claim(claimed.id, "worker-b", now=T0 + timedelta(seconds=61))
    with pytest.raises(Refusal) as gone:
        queue.renew(claimed.id, claimed.lease_attempt, "worker-a", now=T0 + timedelta(seconds=62))
    assert gone.value.code == "not_the_holder", "the work was taken again by someone else"
    assert queue.renew(claimed.id, retaken.lease_attempt, "worker-b", now=T0 + timedelta(seconds=62))


def test_a_finished_item_holds_no_lease_to_renew(queue):
    claimed = held(queue)
    queue.report_result(claimed.id, claimed.lease_attempt, WorkStatus.DONE, attestation=ATT, now=T0)
    with pytest.raises(LeaseError, match="holds no lease"):
        queue.renew(claimed.id, claimed.lease_attempt, "worker-a", now=T0)


def test_renewal_is_capped_at_the_maximum_lease_age(queue):
    queue.lease_max_age = 100
    claimed = held(queue, ttl=60)
    capped = queue.renew(claimed.id, claimed.lease_attempt, "worker-a", ttl_seconds=1000,
                         now=T0 + timedelta(seconds=50))
    assert datetime.fromisoformat(capped.lease_expires_at) == T0 + timedelta(seconds=100), "never past the cap"
    with pytest.raises(Refusal) as exc:
        queue.renew(claimed.id, claimed.lease_attempt, "worker-a", now=T0 + timedelta(seconds=100))
    assert exc.value.code == "lease_max_age" and "100s" in exc.value.message


def test_a_renewal_emits_nothing_on_the_feed(queue):
    seen = []
    claimed = held(queue)
    queue.announce = lambda kind, actor, payload: seen.append(kind)
    queue.renew(claimed.id, claimed.lease_attempt, "worker-a", now=T0 + timedelta(seconds=5))
    assert seen == []


def test_a_transferred_run_refuses_the_old_owners_renewal(library, queue):
    from agentco import ownership

    queue.hub_id, queue.stores = "hub-a", {"runtime-a": frozenset({"rt-agent"})}
    asop = library.create("one step", inputs=[], roles={"w": {"kind": "agent"}},
                          steps=[{"name": "one", "role": "w", "purpose": "p", "gate": GATE}])
    library.activate(asop.asop_id, 1, author_kind="human")
    run = library.run(asop.asop_id, queue, bindings={"w": "rt-agent"}, owner_store="runtime-a", filed_by="rt-agent")
    step = run["steps"][0]["itemId"]
    claimed = queue.claim(step, "rt-agent")
    ownership.transfer(queue, run["runId"], to="hub-a", reason="the runtime is gone", by="dana", at="t")
    with pytest.raises(Refusal) as exc:
        queue.renew(step, claimed.lease_attempt, "rt-agent")
    assert exc.value.code == "not_the_owner"


def test_the_pulse_shows_lease_age(queue):
    from agentco.pulse import lease_ages

    claimed = held(queue, ttl=600)
    queue.renew(claimed.id, claimed.lease_attempt, "worker-a", ttl_seconds=600, now=T0 + timedelta(seconds=30))
    report = lease_ages(queue, now=T0 + timedelta(seconds=120))
    assert report["held"] == 1 and report["maxAgeSeconds"] == queue.lease_max_age
    [lease] = report["oldest"]
    assert lease["itemId"] == claimed.id and lease["ageSeconds"] == 120 and lease["renewals"] == 1


@pytest.mark.parametrize("raw,seconds", [("", 86400), ("90m", 5400), ("2h", 7200), ("3600", 3600), ("1d", 86400)])
def test_the_maximum_age_is_configuration(raw, seconds):
    assert lease_max_age_from_env(raw) == seconds


# --------------------------------------------------------------------------- #
# over HTTP, the relay's transport, and a real socket
# --------------------------------------------------------------------------- #

from tests.test_mcp_remote import KEYS, LoopbackRegistry  # noqa: E402


def make_app(tmp_path, **kw):
    from agentco.app import create_app

    return create_app(db_path=str(tmp_path / "api.sqlite3"), keys=KEYS, operator="operator",
                      work_store=str(tmp_path / "work.jsonl"), sop_store=str(tmp_path / "sops.jsonl"), **kw)


def test_http_renew_and_its_refusals(tmp_path):
    from agentco.publish import RegistryError

    client = TestClient(make_app(tmp_path))
    mine, theirs = LoopbackRegistry("macbook", client), LoopbackRegistry("bigmac", client)
    mine.work_create("long step")
    pulled = mine.work_pull(ttl_seconds=60)
    renewed = mine.work_renew(pulled["item"]["id"], pulled["attempt"], ttl_seconds=900)
    assert renewed["state"] == "renewed" and renewed["attempt"] == pulled["attempt"]
    assert renewed["maxLeaseAgeSeconds"] == 86400 and renewed["leaseAgeSeconds"] is not None
    for call, code in (
        (lambda: theirs.work_renew(pulled["item"]["id"], pulled["attempt"]), "not_the_holder"),
        (lambda: mine._call("POST", f"/work/{pulled['item']['id']}/renew", {}), "attempt_required"),
        (lambda: mine._call("POST", f"/work/{pulled['item']['id']}/renew", {"attempt": 1, "ttl": 5}), "unknown_field"),
        (lambda: mine.work_renew(pulled["item"]["id"], pulled["attempt"] + 5), "work_conflict"),
    ):
        with pytest.raises(RegistryError) as exc:
            call()
        assert exc.value.payload["code"] == code
    done = mine.work_report(pulled["item"]["id"], pulled["attempt"], "done")
    assert done["item"]["status"] == "done"


def test_the_mcp_remote_backend_renews_over_the_wire(tmp_path):
    """No MCP tool (the surface is at its twelve-tool ceiling, ADR 0004); the
    remote backend method is there for when one is decided."""
    from agentco.mcp_server import _RemoteBackend

    client = TestClient(make_app(tmp_path))
    registry = LoopbackRegistry("macbook", client)
    registry.work_create("long step")
    pulled = registry.work_pull(ttl_seconds=60)
    item = _RemoteBackend(registry).work_renew(pulled["item"]["id"], pulled["attempt"], 600)
    assert item["lease_attempt"] == pulled["attempt"] and item["metadata"]["lease_renewed"]["count"] == 1


def test_live_wire_renew_keeps_a_long_step_alive(tmp_path):
    import socket
    import threading

    import uvicorn

    from agentco.publish import Registry

    app = make_app(tmp_path)
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
    try:
        url = f"http://127.0.0.1:{port}"
        worker, other = Registry("macbook", KEYS["macbook"], url), Registry("bigmac", KEYS["bigmac"], url)
        worker.work_create("long step")
        pulled = worker.work_pull(ttl_seconds=2)
        for _ in range(3):  # three heartbeats across what would have been the expiry
            time.sleep(1)
            worker.work_renew(pulled["item"]["id"], pulled["attempt"], ttl_seconds=2)
        assert other.work_pull()["state"] == "empty", "a renewed lease is not reaped"
        assert worker.work_report(pulled["item"]["id"], pulled["attempt"], "done")["item"]["status"] == "done"
    finally:
        server.should_exit = True
        thread.join(timeout=5)
