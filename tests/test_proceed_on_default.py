"""`on_timeout: proceed_on_default` resolves DONE on its declared default (spec v3.6 §3.3).

Found by the live chain proof (2026-10-18): the park-clock sweep landed every
default that was not `pass` as `verify_failed`, so a product question declared
to proceed on its default instead FAILED the step and blocked everything after
it — the opposite of what the author declared. The contract's own
`asop.gates.timeout_resolution` says `proceed_on_default` is
`{"status": "done", "on_default": {...}}`: dependents are released, built on
the declared option, and the default is recorded as taken — never as a verdict.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import asop.gates
import pytest

from agentco import verifiers
from agentco.work import WorkStatus

needs_v36 = pytest.mark.skipif(
    "proceed_on_default" not in getattr(asop.gates, "ON_TIMEOUT", ()),
    reason="installed asop-spec predates v3.6 (0.6.0)",
)

QUESTION = {
    "kind": "human", "check": "which retention period applies?", "verifier": "owner-a",
    "max_park_seconds": 60, "on_timeout": "proceed_on_default",
    "default": {"option": "keep the current period", "consequence": "the purge job is built for it",
                "audience": "product owner"},
}


def parked(queue):
    item = queue.create("decide retention", verify=QUESTION)
    claimed = queue.claim(item.id, "worker-a")
    queue.report_result(item.id, claimed.lease_attempt, WorkStatus.DONE)
    return queue.get(item.id)


def later(seconds=120):
    return datetime.now(timezone.utc) + timedelta(seconds=seconds)


@needs_v36
def test_the_default_is_taken_and_recorded_not_verified(queue):
    item = parked(queue)
    assert item.status is WorkStatus.AWAITING_VERIFY
    swept = verifiers.sweep_park_clocks(queue, now=later())
    assert swept["resolved"][0]["default"] == "proceed_on_default"
    done = queue.get(item.id)
    assert done.status is WorkStatus.DONE
    record = done.metadata["verify_resolution"]
    assert record["on_default"] == QUESTION["default"]
    assert "NOT by a verdict" in record["note"]
    assert done.attestation is None, "a default taken is not evidence"
    assert done.verify_failures == 0


@needs_v36
def test_taking_the_default_releases_dependents(queue):
    question = parked(queue)
    after = queue.create("build on the answer", blocked_by=[question.id])
    assert after.id not in {i.id for i in queue.ready()}
    verifiers.sweep_park_clocks(queue, now=later())
    assert after.id in {i.id for i in queue.ready()}


@needs_v36
def test_before_the_deadline_nothing_moves(queue):
    item = parked(queue)
    verifiers.sweep_park_clocks(queue, now=later(30))
    assert queue.get(item.id).status is WorkStatus.AWAITING_VERIFY


@pytest.mark.parametrize("default,landing", [("pass", WorkStatus.DONE), ("fail", WorkStatus.VERIFY_FAILED)])
def test_the_older_defaults_are_unchanged(queue, default, landing):
    gate = {"kind": "judged", "check": "is it right", "max_park_seconds": 60, "on_timeout": default}
    item = queue.create("judge it", verify=gate)
    claimed = queue.claim(item.id, "worker-a")
    queue.report_result(item.id, claimed.lease_attempt, WorkStatus.DONE)
    verifiers.sweep_park_clocks(queue, now=later())
    assert queue.get(item.id).status is landing
    assert "on_default" not in queue.get(item.id).metadata["verify_resolution"]
