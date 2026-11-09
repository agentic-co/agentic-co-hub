"""Known issue, Finding 4: the verifier registry fails CLOSED (ASOP §5.3, §9).

A `judged` or `human` attestation's `submitted_by` is resolved against the
operator's declared registry before it is accepted. The registry for that role
is `ASOP_VERIFIERS` (routes) plus `ASOP_HUMANS` (people, who verify by being
human, the same reading `may_adjudicate` gives the adjudicator registry). An
identity neither resolves is `unauthenticated`; an UNSET registry authenticates
nobody, not everybody.

Same convention as `tests/test_adversarial_findings.py`: each test that states
a property the code did not hold began `xfail(strict=True)` and loses its marker
in the commit that makes it hold. Tests without a marker are GUARDS — the parts
of the property that already held and must keep holding through the change
(the declared verifier still answers; an enrolled person's signed answer still
lands; the clock path never authenticates anyone because it never attests).

What stays out of scope, deliberately: FILING a judged or human gate with
`on_timeout: pass` while nothing is declared is still allowed. That
configuration is detect-and-report (`verifier_status`), not prevent, and
`test_a_clock_only_queue_never_reads_as_configured` pins it.
"""

from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from agentco import auth
from agentco.app import create_app
from agentco.errors import Refusal
from agentco.work import Queue, WorkStatus

JUDGED = {"kind": "judged", "check": "a reviewer reads the diff", "max_park_seconds": 900,
          "on_timeout": "escalate", "escalate_to": "release-owner"}
HUMAN = {"kind": "human", "check": "a person confirms the rollback was exercised", "verifier": "dana",
         "max_park_seconds": 3600, "on_timeout": "escalate", "escalate_to": "dana"}

UNAUTHENTICATED = "unauthenticated"


def attestation(gate):
    return {"check": gate["check"], "exit_status": 0, "environment": "reviewer laptop",
            "at": "2026-11-09T12:00:00+00:00",
            "verdict": {"passed": True, "reason": "read it and judged the result"}}


def parked(queue, gate):
    item = queue.create("migrate the schema", verify=gate)
    leased = queue.claim(item.id, "kofi")
    queue.report_result(item.id, leased.lease_attempt, WorkStatus.DONE)
    assert queue.get(item.id).status is WorkStatus.AWAITING_VERIFY
    return item.id


@pytest.fixture()
def bare(tmp_path, monkeypatch):
    """A queue with NOTHING declared — not even by an exported variable."""
    for name in ("ASOP_VERIFIERS", "AGENTCO_VERIFIERS", "ASOP_HUMANS", "AGENTCO_HUMANS"):
        monkeypatch.delenv(name, raising=False)
    return Queue(tmp_path / "work.jsonl")


# --------------------------------------------------------------------------- #
# the property
# --------------------------------------------------------------------------- #


@pytest.mark.xfail(strict=True, reason="Finding 4: an undeclared registry authenticates everybody")
def test_an_undeclared_verifier_registry_authenticates_nobody_on_a_judged_gate(bare):
    item_id = parked(bare, JUDGED)
    with pytest.raises(Refusal) as caught:
        bare.attest(item_id, attestation(JUDGED), submitted_by="reviewer-box", capabilities=["verify"])
    assert caught.value.code == UNAUTHENTICATED
    assert bare.get(item_id).status is WorkStatus.AWAITING_VERIFY, "nothing landed"


@pytest.mark.xfail(strict=True, reason="Finding 4: a human gate trusts the gate's own `verifier` string")
def test_an_undeclared_registry_authenticates_nobody_on_a_human_gate(bare):
    """The named person is not a credential: the gate's `verifier` field is written
    by whoever FILED the item, not declared by the operator."""
    item_id = parked(bare, HUMAN)
    with pytest.raises(Refusal) as caught:
        bare.attest(item_id, attestation(HUMAN), submitted_by="dana")
    assert caught.value.code == UNAUTHENTICATED
    assert bare.get(item_id).status is WorkStatus.AWAITING_VERIFY


@pytest.mark.xfail(strict=True, reason="Finding 4: a registry miss is reported as attestation_invalid")
def test_an_attestation_from_outside_the_declared_verifier_set_is_unauthenticated(tmp_path):
    queue = Queue(tmp_path / "work.jsonl", verifiers=["reviewer-box"], humans=[])
    item_id = parked(queue, JUDGED)
    with pytest.raises(Refusal) as caught:
        queue.attest(item_id, attestation(JUDGED), submitted_by="mallory", capabilities=["verify"])
    assert caught.value.code == UNAUTHENTICATED
    # Authenticated by the transport, not resolved by the registry: 403, never
    # 401. A 401 tells a harness its key is wrong, and a model reading that
    # mints itself a new one (`mcp_server.access_steps`).
    assert caught.value.http_status == 403
    assert queue.get(item_id).status is WorkStatus.AWAITING_VERIFY


@pytest.mark.xfail(strict=True, reason="Finding 4: the human gate's named verifier is never resolved")
def test_a_human_gate_naming_an_undeclared_person_is_refused_even_when_others_are_declared(tmp_path):
    queue = Queue(tmp_path / "work.jsonl", verifiers=["reviewer-box"], humans=["eli"])
    item_id = parked(queue, HUMAN)
    with pytest.raises(Refusal) as caught:
        queue.attest(item_id, attestation(HUMAN), submitted_by="dana")
    assert caught.value.code == UNAUTHENTICATED


# --------------------------------------------------------------------------- #
# guards: what must keep working
# --------------------------------------------------------------------------- #


def test_a_declared_verifier_still_answers_a_judged_gate(tmp_path):
    queue = Queue(tmp_path / "work.jsonl", verifiers=["reviewer-box"], humans=[])
    item_id = parked(queue, JUDGED)
    done = queue.attest(item_id, attestation(JUDGED), submitted_by="reviewer-box", capabilities=["verify"])
    assert done is not None and done.status is WorkStatus.DONE


def test_a_declared_human_answers_their_human_gate_with_no_verifier_routes_declared(tmp_path):
    queue = Queue(tmp_path / "work.jsonl", verifiers=[], humans=["dana"])
    item_id = parked(queue, HUMAN)
    done = queue.attest(item_id, attestation(HUMAN), submitted_by="dana")
    assert done is not None and done.status is WorkStatus.DONE


def test_the_park_clock_resolves_without_attesting_so_the_registry_never_gates_it(bare):
    """The clock path (`sweep_park_clocks` -> `resolve_by_default`) is untouched:
    an `on_timeout: pass` gate on a bare queue still resolves on its timer,
    recorded as no check having been run."""
    from datetime import timedelta

    from agentco import verifiers
    from agentco.work import _now

    gate = {**JUDGED, "on_timeout": "pass", "max_park_seconds": 60}
    gate.pop("escalate_to")
    item_id = parked(bare, gate)
    verifiers.sweep_park_clocks(bare, now=_now() + timedelta(seconds=3600))
    item = bare.get(item_id)
    assert item.status is WorkStatus.DONE
    assert not item.attestation, "the clock never grants evidence"


# --------------------------------------------------------------------------- #
# over HTTP: the refusal a non-registered attester sees
# --------------------------------------------------------------------------- #

KEYS = {"kofi": "kofi-secret", "mallory": "mallory-secret", "reviewer-box": "rb-secret", "op": "op-secret"}


def http(client, method, path, actor, body=None):
    raw = json.dumps(body).encode() if body is not None else b""
    ts = str(int(time.time()))
    headers = {"X-AgentCo-Actor": actor, "X-AgentCo-Timestamp": ts,
               "X-AgentCo-Signature": auth.sign(KEYS[actor], method, path, ts, raw),
               "Content-Type": "application/json"}
    return client.request(method, path, content=raw or None, headers=headers)


@pytest.mark.xfail(strict=True, reason="Finding 4: HTTP attest accepts any authenticated actor")
def test_http_an_unregistered_attester_gets_403_unauthenticated(tmp_path, monkeypatch):
    for name in ("ASOP_VERIFIERS", "AGENTCO_VERIFIERS", "ASOP_HUMANS", "AGENTCO_HUMANS"):
        monkeypatch.delenv(name, raising=False)
    app = create_app(db_path=str(tmp_path / "api.sqlite3"), keys=KEYS, operator="op",
                     work_store=str(tmp_path / "work.jsonl"), sop_store=str(tmp_path / "sops.jsonl"))
    client = TestClient(app)
    item_id = parked(app.state.queue, JUDGED)
    refused = http(client, "POST", f"/work/{item_id}/attest", "mallory",
                   {"attestation": attestation(JUDGED), "capabilities": ["verify"]})
    assert (refused.status_code, refused.json()["code"]) == (403, UNAUTHENTICATED), refused.json()
    assert app.state.queue.get(item_id).status is WorkStatus.AWAITING_VERIFY
