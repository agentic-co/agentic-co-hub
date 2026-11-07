"""Findings R5-R8 of the 2026-11-07 review: input handling at the edges.

Each test is `xfail(strict=True)` against the code as reviewed, per the
conventions in `tests/test_adversarial_findings.py`: fix the defect, delete the
marker in the same change.

- R5: a wrong-typed payload field reached `TypeError` / `AttributeError` /
  `OverflowError` / `sqlite3.ProgrammingError` and came back as a 500 "registry
  bug" with the exception text echoed.
- R6: `Queue.claim` never bounded `ttl_seconds`.
- R7: `report_result` honoured an expired lease that `renew` refuses. The
  assertion accepts either consistent resolution (both accept, or both refuse).
- R8: a retried `attest` dropped its adjudication rider.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from agentco import auth, db, leases
from agentco.app import create_app
from agentco.errors import Refusal
from agentco.work import LeaseError, WorkStatus

KEYS = {"dana": "dana-secret", "kofi": "kofi-secret", "operator": "op-secret"}
T0 = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)
GATE = {"kind": "deterministic", "check": "make check", "max_park_seconds": 900, "on_timeout": "fail"}
JUDGED = {"kind": "judged", "check": "a reviewer reads the diff", "max_park_seconds": 900,
          "on_timeout": "escalate", "escalate_to": "release-owner"}
ATT = {"check": "make check", "exit_status": 0, "environment": "ci/synthetic", "at": "2026-10-18T00:00:00+00:00"}


@pytest.fixture()
def client(tmp_path):
    app = create_app(
        db_path=str(tmp_path / "api.sqlite3"), keys=KEYS, operator="operator",
        work_store=str(tmp_path / "work.jsonl"), sop_store=str(tmp_path / "sops.jsonl"),
    )
    return TestClient(app)


def _post(client, path, body, actor="dana"):
    raw = json.dumps(body)
    ts = str(int(time.time()))
    headers = {
        "X-AgentCo-Actor": actor, "X-AgentCo-Timestamp": ts,
        "X-AgentCo-Signature": auth.sign(KEYS[actor], "POST", path, ts, raw.encode()),
        "Content-Type": "application/json",
    }
    return client.post(path, content=raw, headers=headers)


def _refused_cleanly(response, what):
    body = response.json()
    assert response.status_code == 400, f"{what}: expected a 400 refusal, got {response.status_code} {body}"
    assert body.get("code") not in (None, "internal_error"), f"{what}: {body}"
    assert body.get("remediation"), f"{what}: no remediation"
    for leak in ("TypeError", "AttributeError", "OverflowError", "ProgrammingError", "object has no attribute"):
        assert leak not in response.text, f"{what}: exception text echoed: {response.text}"


# --------------------------------------------------------------------------- R5

_CLAIM_OK = {"repo": "acme/web-platform", "prefixes": ["src/budget/grid"], "intent": "implement"}


@pytest.mark.xfail(strict=True, reason="R5: wrong-typed scope-claim fields reach TypeError and return 500")
@pytest.mark.parametrize("field, value", [
    ("repo", 5), ("holder", 5), ("prefixes", [5]), ("prefixes", 5), ("intent", 5),
    ("ttlSeconds", float("inf")), ("ttlSeconds", True), ("ttlSeconds", [1]),
])
def test_a_wrong_typed_scope_claim_field_is_refused_not_a_500(client, field, value):
    response = _post(client, "/scope-claims", {**_CLAIM_OK, field: value})
    _refused_cleanly(response, f"{field}={value!r}")


@pytest.mark.xfail(strict=True, reason="R5: a dict/list release action reaches sqlite3.ProgrammingError")
@pytest.mark.parametrize("action", [{"a": 1}, [1], 5])
def test_a_wrong_typed_release_action_is_refused_not_a_500(client, action):
    lease = _post(client, "/scope-claims", _CLAIM_OK).json()
    response = _post(client, f"/scope-claims/{lease['leaseUid']}/release", {"action": action})
    _refused_cleanly(response, f"action={action!r}")


@pytest.mark.xfail(strict=True, reason="R5: wrong-typed work payload fields reach TypeError and return 500")
@pytest.mark.parametrize("path, body", [
    ("/work/pull", {"capabilities": 5}),
    ("/work/pull", {"capabilities": [5]}),
    ("/work/pull", {"ttlSeconds": float("inf")}),
    ("/work", {"title": "t", "metadata": [1]}),
    ("/work", {"title": 5}),
    ("/work", {"title": "t", "assignedAgent": 5}),
])
def test_a_wrong_typed_work_field_is_refused_not_a_500(client, path, body):
    if path == "/work/pull":
        _post(client, "/work", {"title": "something to pull"})
    _refused_cleanly(_post(client, path, body), f"{path} {body!r}")


@pytest.mark.xfail(strict=True, reason="R5: an infinite ttl raises OverflowError out of the core")
def test_the_core_refuses_a_non_numeric_scope_ttl(tmp_path):
    conn = db.connect(tmp_path / "r.sqlite3")
    for bad in ("abc", float("inf"), float("nan"), True, None, [1]):
        with pytest.raises(Refusal):
            leases.claim(conn, actor="dana", repo="acme/web-platform", prefixes=["src/budget/grid"],
                         intent="implement", ttl_seconds=bad)


@pytest.mark.xfail(strict=True, reason="R5: a malformed events limit is not refused")
@pytest.mark.parametrize("limit", ["inf", "1e999", "nan"])
def test_an_unusable_events_limit_never_500s(client, limit):
    path = "/events"
    ts = str(int(time.time()))
    headers = {"X-AgentCo-Actor": "dana", "X-AgentCo-Timestamp": ts,
               "X-AgentCo-Signature": auth.sign(KEYS["dana"], "GET", path, ts, b"")}
    response = client.get(f"{path}?limit={limit}", headers=headers)
    assert response.status_code != 500, response.text


# --------------------------------------------------------------------------- R6


@pytest.mark.xfail(strict=True, reason="R6: Queue.claim accepts ttl <= 0 and returns 'leased' on a dead lease")
@pytest.mark.parametrize("ttl", [0, -5])
def test_claim_refuses_a_non_positive_ttl(queue, ttl):
    item = queue.create("x")
    with pytest.raises(Refusal):
        queue.claim(item.id, "worker-a", ttl_seconds=ttl, now=T0)
    assert queue.get(item.id).status is WorkStatus.PENDING, "a refused claim leaves the item free"


@pytest.mark.xfail(strict=True, reason="R6: Queue.claim accepts any ttl, so one claim can pin an item for decades")
def test_claim_never_holds_an_item_past_the_maximum_lease_age(queue):
    item = queue.create("x")
    try:
        claimed = queue.claim(item.id, "worker-a", ttl_seconds=10**9, now=T0)
    except Refusal:
        return  # refusing is as honest as clamping
    expires = datetime.fromisoformat(claimed.lease_expires_at)
    assert expires <= T0 + timedelta(seconds=queue.lease_max_age)


@pytest.mark.xfail(strict=True, reason="R6: /work/pull passes an unbounded ttl through")
def test_pull_over_http_bounds_the_ttl(client):
    _post(client, "/work", {"title": "pin me"})
    zero = _post(client, "/work/pull", {"ttlSeconds": 0})
    assert zero.status_code == 400, zero.text
    huge = _post(client, "/work/pull", {"ttlSeconds": 10**9})
    if huge.status_code == 200:
        body = huge.json()
        expires = datetime.fromisoformat(body["item"]["lease_expires_at"])
        assert expires <= datetime.now(timezone.utc) + timedelta(days=2), "pinned for years"


# --------------------------------------------------------------------------- R7


@pytest.mark.xfail(strict=True, reason="R7: report accepts an expired lease that renew refuses")
def test_report_and_renew_agree_about_an_expired_unreclaimed_lease(queue):
    """Same item state, same instant: both operations accept or both refuse."""

    def outcome(operation):
        item = queue.create("long step", verify=GATE)
        claimed = queue.claim(item.id, "worker-a", ttl_seconds=60, now=T0)
        late = T0 + timedelta(seconds=120)
        try:
            if operation == "renew":
                queue.renew(claimed.id, claimed.lease_attempt, "worker-a", ttl_seconds=60, now=late)
            else:
                queue.report_result(claimed.id, claimed.lease_attempt, WorkStatus.DONE,
                                    attestation=ATT, now=late)
        except (LeaseError, Refusal):
            return "refused"
        return "accepted"

    assert outcome("report") == outcome("renew")


# --------------------------------------------------------------------------- R8


@pytest.mark.xfail(strict=True, reason="R8: a retried attest returns early and never applies the rider")
def test_a_retried_attest_completes_a_missing_rider_exactly_once(queue, monkeypatch):
    queue.humans = frozenset({"dana"})
    queue.adjudicators = frozenset()
    item = queue.create("judged", verify=JUDGED)
    leased = queue.claim(item.id, "kofi")
    queue.report_result(item.id, leased.lease_attempt, WorkStatus.DONE)
    verdict = {"check": "a reviewer reads the diff", "exit_status": 0, "environment": "laptop",
               "at": "2026-10-18T00:00:00+00:00",
               "verdict": {"passed": True, "reason": "the reviewer read the diff and judged it fine"}}
    rider = {"verdict": "bad", "evidence": "skipped the reproduce step"}
    call = dict(submitted_by="dana", capabilities=["verify"], adjudication=rider, idempotency_key="k-1")

    real = queue.adjudicate

    def flaky(*args, **kwargs):
        monkeypatch.setattr(queue, "adjudicate", real)  # fails exactly once
        raise OSError("the second write failed")

    monkeypatch.setattr(queue, "adjudicate", flaky)
    try:
        queue.attest(item.id, verdict, **call)
    except OSError:
        pass  # a non-atomic implementation fails here; an atomic one does not

    retried = queue.attest(item.id, verdict, **call)
    assert retried.metadata["adjudication"]["verdict"] == "bad", "the retry lost the rider"
    again = queue.attest(item.id, verdict, **call)  # a double application must be impossible
    assert again.metadata["adjudication"] == retried.metadata["adjudication"]
