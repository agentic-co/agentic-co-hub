"""The work queue and the SOP library, over HTTP.

These primitives existed only on the MCP surface, which opens the JSONL stores
directly and has no remote mode — so a harness on another machine could push a
scope claim to a shared registry but could never pull work from it. That is the
gap these endpoints close, and the tests worth having are the ones that would
fail if the closing were dishonest:

  * **The fence must survive the wire.** A report carrying a stale attempt has
    to be refused as *superseded*, with the work intact under whoever holds it
    now. If HTTP quietly dropped the fence, every test below would still pass
    except this one.
  * **The claiming identity must be the authenticated actor.** A body field
    naming the worker means anyone can take anyone's lease by typing a name,
    and the fence would faithfully record the theft as legitimate.
  * **A refusal must never arrive as a 500.** The repo's own principle is that
    every refusal carries a remediation; `int()` on caller text raising
    ValueError into the generic handler is exactly how that principle is lost,
    and it is already an open defect on four other paths.
  * **An empty queue is not an error.** A poller finding nothing is the
    ordinary shape of a quiet minute.
"""

from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from agentco import auth
from agentco.app import create_app

KEYS = {"dana": "dana-secret", "kofi": "kofi-secret", "operator": "op-secret"}


@pytest.fixture()
def client(tmp_path):
    app = create_app(
        db_path=str(tmp_path / "api.sqlite3"),
        keys=KEYS,
        operator="operator",
        work_store=str(tmp_path / "work.jsonl"),
        sop_store=str(tmp_path / "sops.jsonl"),
    )
    return TestClient(app)


def signed(method: str, path: str, actor: str, body: dict | None = None) -> dict:
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


def create_item(client, actor="dana", **fields):
    body = {"title": "rebuild the invoice exporter", **fields}
    response = post(client, "/work", actor, body)
    assert response.status_code == 200, response.text
    return response.json()["item"]


# --------------------------------------------------------------------------- #
# the round trip the two-machine case depends on
# --------------------------------------------------------------------------- #


def test_one_actor_files_work_and_another_pulls_and_reports_it(client):
    """The whole point: work filed by one identity, run by a different one."""
    item = create_item(client)

    pulled = post(client, "/work/pull", "kofi", {}).json()
    assert pulled["state"] == "leased"
    assert pulled["item"]["id"] == item["id"]
    assert pulled["item"]["leased_by"] == "kofi"
    assert pulled["attempt"] == pulled["item"]["lease_attempt"]

    reported = post(
        client,
        f"/work/{item['id']}/report",
        "kofi",
        {"attempt": pulled["attempt"], "status": "done", "result": "exporter rebuilt"},
    )
    assert reported.status_code == 200, reported.text
    assert reported.json()["item"]["status"] == "done"

    # And the filer can see the outcome without sharing a filesystem with the worker.
    listed = get(client, "/work", "dana", "?status=done").json()["items"]
    assert [i["id"] for i in listed] == [item["id"]]
    assert listed[0]["result"] == "exporter rebuilt"


def test_a_body_that_names_its_own_actor_is_refused_not_quietly_ignored(client):
    """Silently dropping the field leaves the caller believing it took effect.

    Ignoring `actor` would be safe and unreadable: the lease would be correct
    and the caller would go on thinking they had claimed as dana. The same
    argument the SOP library makes about repairing a malformed payload applies
    — the caller believes they wrote one thing and the store holds another.
    """
    create_item(client)
    refused = post(client, "/work/pull", "kofi", {"actor": "dana"})
    assert refused.status_code == 400
    body = refused.json()
    assert body["code"] == "actor_in_body"
    assert "agentLabel" in body["remediation"], "a refusal must name the thing to do instead"


def test_the_claiming_identity_is_the_authenticated_actor_not_the_payload(client):
    """A worker must not be able to name itself into someone else's lease."""
    create_item(client)
    pulled = post(client, "/work/pull", "kofi", {"agent": "dana"}).json()
    assert pulled["item"]["leased_by"] == "kofi", (
        "the body named dana and the server believed it — any actor could now "
        "take any other actor's lease, and the fence would record it as valid"
    )


def test_an_empty_queue_answers_empty_rather_than_failing(client):
    pulled = post(client, "/work/pull", "kofi", {})
    assert pulled.status_code == 200
    assert pulled.json() == {"state": "empty", "item": None}


# --------------------------------------------------------------------------- #
# the fence — the assertion the wire could silently drop
# --------------------------------------------------------------------------- #


def test_a_report_on_a_stale_attempt_is_refused_as_superseded(client):
    item = create_item(client)
    first = post(client, "/work/pull", "kofi", {}).json()

    stale = post(
        client,
        f"/work/{item['id']}/report",
        "kofi",
        {"attempt": first["attempt"] - 1, "status": "done", "result": "late"},
    )
    assert stale.status_code == 409, stale.text
    payload = stale.json()
    assert payload["code"] == "work_conflict"
    assert payload["remediation"].strip()

    # Refused means nothing was written — the item is still leased, not done.
    still = get(client, "/work", "dana").json()["items"][0]
    assert still["status"] == "in_progress"
    assert still["result"] is None


def test_a_report_without_the_fence_is_refused_before_anything_is_written(client):
    item = create_item(client)
    post(client, "/work/pull", "kofi", {})
    response = post(client, f"/work/{item['id']}/report", "kofi", {"status": "done"})
    assert response.status_code == 400
    assert response.json()["code"] == "attempt_required"
    assert "attempt" in response.json()["remediation"]


def test_an_idempotent_retry_of_the_same_report_is_safe(client):
    """Any transport can lose an acknowledgement after the server applied it."""
    item = create_item(client)
    pulled = post(client, "/work/pull", "kofi", {}).json()
    body = {
        "attempt": pulled["attempt"],
        "status": "done",
        "result": "exporter rebuilt",
        "idempotencyKey": "retry-1",
    }
    first = post(client, f"/work/{item['id']}/report", "kofi", body)
    second = post(client, f"/work/{item['id']}/report", "kofi", body)
    assert first.status_code == 200
    assert second.status_code == 200, second.text
    assert second.json()["item"]["status"] == "done"


# --------------------------------------------------------------------------- #
# refusals, none of which may arrive as a 500
# --------------------------------------------------------------------------- #


def test_reporting_against_an_unknown_item_is_a_404_with_a_remediation(client):
    response = post(
        client, "/work/w-deadbeef/report", "kofi", {"attempt": 1, "status": "done"}
    )
    assert response.status_code == 404
    assert response.json()["code"] == "work_item_unknown"
    assert response.json()["remediation"].strip()


def test_a_non_terminal_outcome_is_refused_and_names_the_legal_ones(client):
    item = create_item(client)
    pulled = post(client, "/work/pull", "kofi", {}).json()
    response = post(
        client,
        f"/work/{item['id']}/report",
        "kofi",
        {"attempt": pulled["attempt"], "status": "in_progress"},
    )
    assert response.status_code == 400
    assert response.json()["code"] == "not_terminal"
    assert "done" in response.json()["remediation"]
    assert "failed" in response.json()["remediation"]


@pytest.mark.parametrize(
    "body",
    [
        {"ttlSeconds": "soon"},
        {"ttlSeconds": []},
    ],
)
def test_a_non_numeric_ttl_is_refused_rather_than_reported_as_a_registry_bug(client, body):
    """The failure mode this endpoint must not reproduce.

    Four existing paths answer a malformed number with HTTP 500 and the words
    "this is a registry bug", which tells someone who sent a typo that the
    server is broken. New surface does not get to add a fifth.
    """
    response = post(client, "/work/pull", "kofi", body)
    assert response.status_code == 400, response.text
    assert response.json()["code"] == "not_an_integer"
    assert "registry bug" not in json.dumps(response.json())


def test_an_unknown_status_filter_is_refused_with_the_legal_set(client):
    response = get(client, "/work", "dana", "?status=nearly")
    assert response.status_code == 400
    assert response.json()["code"] == "unknown_status"
    assert "pending" in response.json()["remediation"]


def test_a_duplicate_natural_key_returns_the_existing_item(client):
    first = create_item(client, naturalKey="invoice-exporter")
    second = create_item(client, title="something else entirely", naturalKey="invoice-exporter")
    assert second["id"] == first["id"]
    assert second["title"] == first["title"]


# --------------------------------------------------------------------------- #
# SOPs — a lesson learned on one machine, readable on every other
# --------------------------------------------------------------------------- #


def create_sop(client, actor="dana"):
    response = post(
        client,
        "/sops",
        actor,
        {
            "title": "Restore a stalled export",
            "purpose": "Get a stuck export moving without losing the partial run",
            "trigger": "An export sits in_progress past its lease",
            "definition_of_done": "The export reports done or failed, with a reason",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["sop"]


def test_a_draft_is_not_served_as_active_until_it_is_activated(client):
    """Activation is a separate act: a procedure should be readable by the
    person who will follow it before it starts producing work for them."""
    sop = create_sop(client)
    assert sop["status"] == "draft"
    assert get(client, f"/sops/{sop['sop_id']}", "kofi").json()["sop"] is None

    post(client, f"/sops/{sop['sop_id']}/activate", "dana", {"version": 1})
    served = get(client, f"/sops/{sop['sop_id']}", "kofi").json()["sop"]
    assert served["version"] == 1
    assert served["status"] == "active"


def test_a_lesson_learned_reaches_a_second_reader_as_a_new_active_version(client):
    """The cross-machine case, minus the machines: dana revises, kofi reads it."""
    sop = create_sop(client)
    post(client, f"/sops/{sop['sop_id']}/activate", "dana", {"version": 1})

    revised = post(
        client,
        f"/sops/{sop['sop_id']}/revise",
        "dana",
        {"common_mistakes": ["Re-running the export before releasing the stale lease"]},
    ).json()["sop"]
    assert revised["version"] == 2
    post(client, f"/sops/{sop['sop_id']}/activate", "dana", {"version": 2})

    read = get(client, f"/sops/{sop['sop_id']}", "kofi").json()["sop"]
    assert read["version"] == 2
    assert read["common_mistakes"] == ["Re-running the export before releasing the stale lease"]
    # Unset fields carry forward — a one-line lesson must not blank the rest.
    assert read["purpose"] == sop["purpose"]
    assert read["trigger"] == sop["trigger"]


def test_a_superseded_version_stays_readable_by_the_instances_pinned_to_it(client):
    sop = create_sop(client)
    post(client, f"/sops/{sop['sop_id']}/activate", "dana", {"version": 1})
    post(client, f"/sops/{sop['sop_id']}/revise", "dana", {"inputs": "the export id"})
    post(client, f"/sops/{sop['sop_id']}/activate", "dana", {"version": 2})

    pinned = get(client, f"/sops/{sop['sop_id']}", "kofi", "?version=1").json()["sop"]
    assert pinned["version"] == 1
    assert pinned["status"] == "superseded"


def test_the_active_list_is_what_a_second_machine_discovers(client):
    sop = create_sop(client)
    assert get(client, "/sops", "kofi").json()["sops"] == []
    post(client, f"/sops/{sop['sop_id']}/activate", "dana", {"version": 1})
    listed = get(client, "/sops", "kofi").json()["sops"]
    assert [s["sop_id"] for s in listed] == [sop["sop_id"]]


def test_an_unknown_sop_reads_as_null_rather_than_failing(client):
    """Resolving a pin must never fail loudly, or a caller stops asking."""
    response = get(client, "/sops/sop-deadbeef", "kofi")
    assert response.status_code == 200
    assert response.json()["sop"] is None


def test_activating_a_version_that_does_not_exist_is_refused_with_a_remediation(client):
    sop = create_sop(client)
    response = post(client, f"/sops/{sop['sop_id']}/activate", "dana", {"version": 9})
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "sop_refused"
    assert response.json()["remediation"].strip()


def test_activate_without_a_version_is_refused_rather_than_guessing(client):
    sop = create_sop(client)
    response = post(client, f"/sops/{sop['sop_id']}/activate", "dana", {})
    assert response.status_code == 400
    assert response.json()["code"] == "version_required"


# --------------------------------------------------------------------------- #
# Machine-exclusive work — enforced, not merely routed
# --------------------------------------------------------------------------- #


def test_work_requiring_a_capability_is_invisible_to_a_worker_without_it(client):
    """`assignedAgent` is routing. `requires` is the gate, and it fails closed.

    Assignment alone would be visibility: an item assigned to one worker is
    simply not offered to another. That is enough until something claims by a
    route that does not consult the assignment, at which point the only thing
    standing between a machine and work it physically cannot run is a filter.
    The capability check lives inside `claim()`, under the same lock as the CAS.
    """
    post(client, "/work", "dana", {
        "title": "build the MCP tool",
        "assignedAgent": "kofi",
        "requires": ["billing-erp"],
    })

    # The wrong machine, even declaring the capability, is not offered work
    # assigned to someone else.
    assert post(client, "/work/pull", "dana", {"capabilities": ["billing-erp"]}).json()["state"] == "empty"

    # The right machine, declaring nothing, is refused by the gate — closed.
    assert post(client, "/work/pull", "kofi", {}).json()["state"] == "empty"

    # The right machine declaring the capability gets it.
    pulled = post(client, "/work/pull", "kofi", {"capabilities": ["billing-erp"]}).json()
    assert pulled["state"] == "leased"
    assert pulled["item"]["requires"] == ["billing-erp"]


def test_an_sop_instance_carries_the_pin_and_the_capability(client):
    sop = create_sop(client)
    post(client, f"/sops/{sop['sop_id']}/activate", "dana", {"version": 1})

    filed = post(client, f"/sops/{sop['sop_id']}/instantiate", "dana", {
        "title": "[User Story 91166] Expose Client COA",
        "assignedAgent": "kofi",
        "requires": ["billing-erp"],
        "source": "ado",
        "sourceId": "example-org/91166",
    }).json()["item"]
    assert filed["metadata"]["sop_ref"] == {"sop_id": sop["sop_id"], "version": 1}
    assert filed["requires"] == ["billing-erp"]

    # And the worker that pulls it can read the procedure it is pinned to.
    pulled = post(client, "/work/pull", "kofi", {"capabilities": ["billing-erp"]}).json()["item"]
    ref = pulled["metadata"]["sop_ref"]
    procedure = get(client, f"/sops/{ref['sop_id']}", "kofi", f"?version={ref['version']}").json()["sop"]
    assert procedure["definition_of_done"]


def test_instantiating_a_draft_is_refused_across_http(client):
    """The check that makes instantiate worth having as an endpoint."""
    sop = create_sop(client)
    response = post(client, f"/sops/{sop['sop_id']}/instantiate", "dana", {"title": "too early"})
    assert response.status_code == 422
    assert "draft" in response.json()["message"].lower()
    assert response.json()["remediation"].strip()


# --------------------------------------------------------------------------- #
# FIX-L3.10 / FIX-L3.11 over the wire — the commit that fixed them claimed these
# tests and had not written them
# --------------------------------------------------------------------------- #

JUDGED_GATE = {
    "kind": "judged",
    "check": "a reviewer confirms the rollback ran",
    "max_park_seconds": 3600,
    "on_timeout": "escalate",
    "escalate_to": "release-owner",
}


def test_a_report_from_an_actor_who_does_not_hold_the_lease_is_refused(client):
    """The attempt number is in every `GET /work` response, so dana can read
    kofi's fence and report kofi's item at it. That report used to be recorded as
    kofi's completion — and dana could then attest it."""
    item = create_item(client, verify=JUDGED_GATE)
    pulled = post(client, "/work/pull", "kofi", {}).json()
    assert pulled["item"]["leased_by"] == "kofi"

    hijack = post(client, f"/work/{item['id']}/report", "dana",
                  {"attempt": pulled["attempt"], "status": "done"})
    assert hijack.status_code >= 400, hijack.text
    assert "held by 'kofi'" in hijack.json()["message"]

    still = get(client, "/work", "dana").json()["items"]
    [row] = [i for i in still if i["id"] == item["id"]]
    assert row["status"] == "in_progress" and row["leased_by"] == "kofi"


def test_a_report_on_an_unclaimed_item_is_refused_over_http(client):
    """No lease, no report. Attempt 0 matched attempt 0 on an item nobody held,
    and the parked item's executor was None — which the separation check on a
    judged gate could never see."""
    item = create_item(client, verify=JUDGED_GATE)
    ghost = post(client, f"/work/{item['id']}/report", "dana", {"attempt": 0, "status": "done"})
    assert ghost.status_code >= 400, ghost.text
    assert "nobody holds it" in ghost.json()["message"]

    attest = post(client, f"/work/{item['id']}/attest", "dana", {
        "capabilities": ["verify"],
        "attestation": {"check": JUDGED_GATE["check"], "exit_status": 0,
                        "environment": "dana's laptop", "at": "2026-09-02T12:00:00+00:00"},
    })
    assert attest.status_code >= 400, "nothing was parked, so there is nothing to attest"
