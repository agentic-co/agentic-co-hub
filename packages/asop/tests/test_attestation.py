"""The evidence side of the gate contract — `validate_attestation`,
`attestation_passes`, `retry_decision` — ported from the plane's
`tests/test_gates.py` and called directly against a hand-built gate dict
rather than through a queue.
"""

from __future__ import annotations

import pytest

from asop import gates
from asop.errors import Refusal

GATE = {"kind": "deterministic", "check": "pytest -q", "checks": None}


def attestation(check: str = "pytest -q", exit_status: int = 0) -> dict:
    return {
        "check": check,
        "exit_status": exit_status,
        "environment": "ci/ubuntu-24.04/py3.12",
        "at": "2026-09-01T12:00:00+00:00",
    }


def test_a_passing_attestation_normalises_and_reports_pass():
    record = gates.validate_attestation(attestation(), gate=GATE, submitted_by="worker-a")
    assert record["submitted_by"] == "worker-a"
    assert gates.attestation_passes(record) is True


def test_a_failing_attestation_reports_fail():
    record = gates.validate_attestation(attestation(exit_status=1), gate=GATE, submitted_by="worker-a")
    assert gates.attestation_passes(record) is False


def test_an_attestation_for_a_different_check_is_refused():
    with pytest.raises(Refusal) as caught:
        gates.validate_attestation(
            attestation(check="pytest -k the_one_that_passes"), gate=GATE, submitted_by="worker-a"
        )
    assert caught.value.code == gates.ATTESTATION_INVALID


def test_the_body_cannot_name_the_submitter():
    with pytest.raises(Refusal):
        gates.validate_attestation(
            dict(attestation(), submitted_by="somebody-trusted"), gate=GATE, submitted_by="worker-a"
        )


def test_an_unknown_attestation_field_is_refused():
    with pytest.raises(Refusal) as caught:
        gates.validate_attestation(dict(attestation(), extra="nope"), gate=GATE, submitted_by="worker-a")
    assert "unknown attestation field(s)" in caught.value.message


def test_exit_status_rejects_a_bool():
    with pytest.raises(Refusal):
        gates.validate_attestation(dict(attestation(), exit_status=True), gate=GATE, submitted_by="worker-a")


def test_environment_is_required():
    payload = attestation()
    payload["environment"] = "  "
    with pytest.raises(Refusal):
        gates.validate_attestation(payload, gate=GATE, submitted_by="worker-a")


def test_at_is_required():
    payload = attestation()
    payload["at"] = ""
    with pytest.raises(Refusal):
        gates.validate_attestation(payload, gate=GATE, submitted_by="worker-a")


def test_an_attestation_that_is_not_a_dict_is_refused():
    with pytest.raises(Refusal):
        gates.validate_attestation("pytest -q", gate=GATE, submitted_by="worker-a")


def test_the_retry_policy_stops_at_two():
    """One fix item, then a human, then never again autonomously."""
    assert gates.retry_decision(1) == "fix"
    assert gates.retry_decision(2) == "escalate"
    assert gates.retry_decision(3) == "stop"
    assert gates.retry_decision(17) == "stop"
    with pytest.raises(ValueError):
        gates.retry_decision(0)
