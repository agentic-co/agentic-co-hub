"""Revision proposals accumulating against the template.

ASOP § 3: good divergence feeds the next version; bad divergence feeds
root-cause; the loop closes on a cadence — captured per run, revised
deliberately, never silently. `SopLibrary.proposals` is the accumulated view
over the adjudications; `SopLibrary.propose` is the deliberate step that turns
the pending ones into a DRAFT — through `revise`, so the revision policy holds,
and never through `activate`.

The gate on this unit reads: a second party confirms a good adjudication
reaches the next version and a bad one reaches root-cause, and that neither
silently edits v1. Those three are the first three tests.
"""

from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from agentco import auth, cli
from agentco.app import create_app
from agentco.policy import AGENT, HUMAN, RevisionPolicyError
from agentco.sop import MAX_COMMON_MISTAKES, PROPOSED_KEY, SopError, SopStatus
from agentco.work import WorkStatus


def procedure(library, **over):
    body = {"definition_of_done": "the export matches the fixture", "validation": "diff -q out.csv expected.csv"}
    body.update(over)
    sop = library.create("export the ledger", author="dana", author_kind=HUMAN, **body)
    library.activate(sop.sop_id, 1, author="dana", author_kind=HUMAN)
    return sop


HUMAN_GATE = {"kind": "human", "check": "the owner signs off", "verifier": "dana",
              "max_park_seconds": 86400, "on_timeout": "escalate", "escalate_to": "dana"}


def adjudicated(library, queue, sop, verdict, evidence, executor="kofi", adjudicator="dana"):
    # A protected step's instances must carry a human gate (P4.0) — the pass
    # under test is downstream of that rule, not exempt from it.
    gate = {"verify": HUMAN_GATE} if set(sop.tags) & library.protected_tags else {}
    item = library.instantiate(sop.sop_id, queue, **gate)
    leased = queue.claim(item.id, executor)
    queue.report_result(item.id, leased.lease_attempt, WorkStatus.DONE)
    queue.adjudicate(item.id, verdict, evidence, adjudicator=adjudicator)
    return queue.get(item.id)


# --------------------------------------------------------------------------- #
# the gate's three sentences
# --------------------------------------------------------------------------- #


def test_a_good_adjudication_reaches_the_next_version(library, queue):
    sop = procedure(library)
    item = adjudicated(library, queue, sop, "good", "the diff is redundant: the export is byte-stable")
    draft = library.propose(sop.sop_id, queue, author="agentco-lessons", author_kind=AGENT)

    assert draft.version == 2
    assert draft.status == SopStatus.DRAFT, "proposed, never activated"
    assert len(draft.proposals) == 1
    assert draft.proposals[0].startswith("the diff is redundant: the export is byte-stable (adjudicated good on")
    assert item.id in draft.proposals[0] and "v1 was wrong here" in draft.proposals[0]
    assert draft.definition_of_done == "the export matches the fixture", "the plane does not author prose"


def test_a_bad_adjudication_reaches_root_cause_and_the_lesson_channel(library, queue):
    sop = procedure(library)
    item = adjudicated(library, queue, sop, "bad", "skipped the diff and reported done")
    view = library.proposals(sop.sop_id, queue)
    assert [e["itemId"] for e in view["rootCause"]] == [item.id]
    assert view["revisions"] == []

    draft = library.propose(sop.sop_id, queue, author="agentco-lessons", author_kind=AGENT)
    assert draft.common_mistakes == [f"skipped the diff and reported done (adjudicated bad on {item.id} by dana)"]
    assert draft.proposals == []


def test_neither_silently_edits_v1(library, queue):
    sop = procedure(library)
    before = library.get(sop.sop_id, version=1).to_json()
    adjudicated(library, queue, sop, "good", "g")
    adjudicated(library, queue, sop, "bad", "b")
    draft = library.propose(sop.sop_id, queue, author="agentco-lessons", author_kind=AGENT)

    v1 = library.get(sop.sop_id, version=1)
    assert json.loads(v1.to_json()) == {**json.loads(before), "superseded_by": draft.version}, (
        "v1 is byte-identical but for the pointer to the draft that follows it"
    )
    assert v1.status == SopStatus.ACTIVE, "still the version instances get"
    assert library.get(sop.sop_id).version == 1
    assert draft.version == 2 and draft.status == SopStatus.DRAFT


# --------------------------------------------------------------------------- #
# accumulation
# --------------------------------------------------------------------------- #


def test_a_proposal_is_consumed_once(library, queue):
    sop = procedure(library)
    item = adjudicated(library, queue, sop, "good", "g")
    draft = library.propose(sop.sop_id, queue, author="bot", author_kind=AGENT)
    assert queue.get(item.id).metadata["adjudication"][PROPOSED_KEY] == draft.version
    assert library.proposals(sop.sop_id, queue)["pending"] == 0
    assert library.propose(sop.sop_id, queue, author="bot", author_kind=AGENT) is None, "nothing pending, quiet run"


def test_proposals_accumulate_across_drafts_until_a_reviser_addresses_them(library, queue):
    sop = procedure(library)
    adjudicated(library, queue, sop, "good", "first")
    library.propose(sop.sop_id, queue, author="bot", author_kind=AGENT)
    adjudicated(library, queue, sop, "good", "second")
    draft = library.propose(sop.sop_id, queue, author="bot", author_kind=AGENT)
    assert [p.split(" (")[0] for p in draft.proposals] == ["first", "second"], "carried forward, then extended"

    # A human addresses them: revises the prose and clears the list.
    fixed = library.revise(sop.sop_id, definition_of_done="the export is byte-stable; no diff needed",
                           proposals=None, author="dana", author_kind=HUMAN)
    assert fixed.proposals == []
    # ...and an agent cannot bring a dismissed proposal back (rule 3).
    with pytest.raises(RevisionPolicyError):
        library.revise(sop.sop_id, proposals=draft.proposals, author="bot", author_kind=AGENT)


def test_open_proposals_survive_an_unrelated_revision(library, queue):
    """A reviser fixing something else must not silently lose the open
    proposals — they carry forward like every other field, until somebody
    addresses or dismisses them on purpose."""
    sop = procedure(library)
    adjudicated(library, queue, sop, "good", "first")
    draft = library.propose(sop.sop_id, queue, author="bot", author_kind=AGENT)
    later = library.revise(sop.sop_id, inputs="the ledger export id", author="dana", author_kind=HUMAN)
    assert later.proposals == draft.proposals
    assert later.inputs == "the ledger export id"


def test_the_view_reports_what_is_pending_and_what_was_consumed(library, queue):
    sop = procedure(library)
    a = adjudicated(library, queue, sop, "good", "g1")
    draft = library.propose(sop.sop_id, queue, author="bot", author_kind=AGENT)
    b = adjudicated(library, queue, sop, "bad", "b1")
    view = library.proposals(sop.sop_id, queue)
    assert view["activeVersion"] == 1 and view["latestVersion"] == draft.version
    assert view["latestStatus"] == "draft"
    assert view["pending"] == 1
    assert {e["itemId"]: e["proposedIn"] for e in view["revisions"]} == {a.id: draft.version}
    assert {e["itemId"]: e["proposedIn"] for e in view["rootCause"]} == {b.id: None}
    assert view["openProposals"] == draft.proposals


def test_an_unadjudicated_instance_proposes_nothing(library, queue):
    sop = procedure(library)
    item = library.instantiate(sop.sop_id, queue)
    leased = queue.claim(item.id, "kofi")
    queue.report_result(item.id, leased.lease_attempt, WorkStatus.DONE)
    assert library.proposals(sop.sop_id, queue)["pending"] == 0
    assert library.propose(sop.sop_id, queue, author="bot", author_kind=AGENT) is None


def test_an_unknown_procedure_is_refused(library, queue):
    with pytest.raises(SopError):
        library.proposals("sop-deadbeef", queue)


# --------------------------------------------------------------------------- #
# the draft is an agent revision — the policy holds
# --------------------------------------------------------------------------- #


def test_a_protected_procedure_cannot_be_drafted_by_the_pass(library, queue):
    sop = procedure(library, tags=["money"])
    adjudicated(library, queue, sop, "good", "g")
    with pytest.raises(RevisionPolicyError) as caught:
        library.propose(sop.sop_id, queue, author="bot", author_kind=AGENT)
    assert caught.value.rule == "protected"
    assert library.history(sop.sop_id)[-1].version == 1, "nothing drafted"
    assert library.proposals(sop.sop_id, queue)["pending"] == 1, "and nothing consumed"


def test_a_human_running_the_pass_is_exempt(library, queue):
    sop = procedure(library, tags=["money"])
    adjudicated(library, queue, sop, "good", "g")
    draft = library.propose(sop.sop_id, queue, author="dana", author_kind=HUMAN)
    assert draft.version == 2 and draft.author_kind == HUMAN


def test_a_lesson_a_human_removed_does_not_come_back_through_the_pass(library, queue):
    sop = procedure(library, common_mistakes=["testing on prod"])
    library.revise(sop.sop_id, common_mistakes=None, author="dana", author_kind=HUMAN)
    library.activate(sop.sop_id, 2, author="dana", author_kind=HUMAN)
    item = adjudicated(library, queue, sop, "bad", "x")
    # Forge the exact lesson text a human removed, to prove the policy sees it.
    record = dict(queue.get(item.id).metadata["adjudication"])
    record["evidence"] = "testing on prod"
    queue.annotate(item.id, {"adjudication": record})
    # The pass appends "(adjudicated bad on ...)" so the text differs; what the
    # policy guards is the human's exact removal. Prove that path directly:
    with pytest.raises(RevisionPolicyError):
        library.revise(sop.sop_id, common_mistakes=["testing on prod"], author="bot", author_kind=AGENT)


def test_the_lesson_channel_cap_is_a_human_decision(library, queue):
    sop = procedure(library, common_mistakes=[f"known mistake {i}" for i in range(MAX_COMMON_MISTAKES)])
    adjudicated(library, queue, sop, "bad", "one more")
    with pytest.raises(SopError) as caught:
        library.propose(sop.sop_id, queue, author="bot", author_kind=AGENT)
    assert "human's call" in str(caught.value)
    assert library.history(sop.sop_id)[-1].version == 1
    assert library.proposals(sop.sop_id, queue)["pending"] == 1, "a refused pass consumes nothing"


def test_the_pass_never_activates(library, queue):
    sop = procedure(library)
    adjudicated(library, queue, sop, "good", "g")
    adjudicated(library, queue, sop, "bad", "b")
    library.propose(sop.sop_id, queue, author="bot", author_kind=AGENT)
    assert library.get(sop.sop_id).version == 1
    assert library.instantiate(sop.sop_id, queue).metadata["sop_ref"]["version"] == 1


# --------------------------------------------------------------------------- #
# over HTTP and the CLI
# --------------------------------------------------------------------------- #

KEYS = {"kofi": "kofi-secret", "dana": "dana-secret", "bot": "bot-secret", "operator": "op-secret"}


def _post(client, path, actor, body):
    raw = json.dumps(body).encode()
    ts = str(int(time.time()))
    return client.post(path, content=raw, headers={
        "X-AgentCo-Actor": actor, "X-AgentCo-Timestamp": ts,
        "X-AgentCo-Signature": auth.sign(KEYS[actor], "POST", path, ts, raw),
        "Content-Type": "application/json",
    })


def _get(client, path, actor):
    ts = str(int(time.time()))
    return client.get(path, headers={
        "X-AgentCo-Actor": actor, "X-AgentCo-Timestamp": ts,
        "X-AgentCo-Signature": auth.sign(KEYS[actor], "GET", path, ts, b""),
    })


def _http_setup(tmp_path, **over):
    client = TestClient(create_app(
        db_path=str(tmp_path / "api.sqlite3"), keys=KEYS, operator="operator",
        work_store=str(tmp_path / "work.jsonl"), sop_store=str(tmp_path / "sops.jsonl"),
        humans=["dana"], **over,
    ))
    sop = _post(client, "/sops", "dana", {"title": "export", "definition_of_done": "matches", **over.get("sop", {})}).json()["sop"]
    assert _post(client, f"/sops/{sop['sop_id']}/activate", "dana", {"version": 1}).status_code == 200
    item = _post(client, f"/sops/{sop['sop_id']}/instantiate", "dana", {}).json()["item"]
    pulled = _post(client, "/work/pull", "kofi", {}).json()
    assert _post(client, f"/work/{item['id']}/report", "kofi", {"attempt": pulled["attempt"], "status": "done"}).status_code == 200
    assert _post(client, f"/work/{item['id']}/adjudicate", "dana", {"verdict": "good", "evidence": "step 2 is redundant"}).status_code == 200
    return client, sop, item


def test_over_http_the_pass_drafts_and_the_view_shows_it(tmp_path):
    client, sop, item = _http_setup(tmp_path)
    view = _get(client, f"/sops/{sop['sop_id']}/proposals", "kofi").json()
    assert view["pending"] == 1 and view["revisions"][0]["itemId"] == item["id"]

    drafted = _post(client, f"/sops/{sop['sop_id']}/propose", "bot", {})
    assert drafted.status_code == 200, drafted.text
    assert drafted.json()["state"] == "drafted"
    draft = drafted.json()["sop"]
    assert draft["version"] == 2 and draft["status"] == "draft" and draft["author_kind"] == AGENT
    assert "step 2 is redundant" in draft["proposals"][0]

    again = _post(client, f"/sops/{sop['sop_id']}/propose", "bot", {})
    assert again.json() == {"state": "nothing_pending", "sop": None}
    assert _get(client, f"/sops/{sop['sop_id']}", "kofi").json()["sop"]["version"] == 1, "not activated"


def test_over_http_a_protected_procedure_refuses_the_agents_pass(tmp_path):
    client = TestClient(create_app(
        db_path=str(tmp_path / "api.sqlite3"), keys=KEYS, operator="operator",
        work_store=str(tmp_path / "work.jsonl"), sop_store=str(tmp_path / "sops.jsonl"), humans=["dana"],
    ))
    sop = _post(client, "/sops", "dana", {"title": "pay", "definition_of_done": "paid", "tags": ["money"]}).json()["sop"]
    _post(client, f"/sops/{sop['sop_id']}/activate", "dana", {"version": 1})
    filed = _post(client, f"/sops/{sop['sop_id']}/instantiate", "dana", {"verify": HUMAN_GATE})
    assert filed.status_code == 200, filed.text
    item = filed.json()["item"]
    pulled = _post(client, "/work/pull", "kofi", {}).json()
    _post(client, f"/work/{item['id']}/report", "kofi", {"attempt": pulled["attempt"], "status": "done"})
    _post(client, f"/work/{item['id']}/adjudicate", "dana", {"verdict": "good", "evidence": "x"})
    refused = _post(client, f"/sops/{sop['sop_id']}/propose", "bot", {})
    assert refused.status_code == 403
    assert refused.json()["code"] == "revision_policy:protected"
    assert _post(client, f"/sops/{sop['sop_id']}/propose", "dana", {}).json()["state"] == "drafted"


def test_the_cli_pass_reports_and_drafts(tmp_path, capsys, monkeypatch):
    from agentco.sop import SopLibrary
    from agentco.work import Queue

    queue = Queue(tmp_path / "work.jsonl")
    library = SopLibrary(tmp_path / "sops.jsonl")
    sop = procedure(library)
    adjudicated(library, queue, sop, "bad", "reported done without running the diff")
    monkeypatch.delenv("AGENTCO_HUMANS", raising=False)
    monkeypatch.delenv("AGENTCO_ACTOR", raising=False)

    code = cli.main(["lessons", "--work-store", str(tmp_path / "work.jsonl"),
                     "--sop-store", str(tmp_path / "sops.jsonl")])
    out = capsys.readouterr().out
    assert code == 0
    assert "1 adjudication(s) pending" in out and "bad " in out

    code = cli.main(["lessons", "--work-store", str(tmp_path / "work.jsonl"),
                     "--sop-store", str(tmp_path / "sops.jsonl"), "--propose", "--json"])
    report = json.loads(capsys.readouterr().out)
    assert code == 0
    drafted = report["sops"][0]["drafted"]
    assert drafted["version"] == 2 and drafted["author"] == "agentco-lessons" and drafted["author_kind"] == AGENT
    assert drafted["common_mistakes"][0].startswith("reported done without running the diff")
    assert library.get(sop.sop_id).version == 1, "the CLI never activates either"
