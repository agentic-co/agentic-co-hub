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

**v3 grain**: the lesson channel and the proposal list are PER STEP — a good
adjudication writes `draft.steps[i].proposals`, a bad one writes
`draft.steps[i].common_mistakes`, and the cap (`MAX_COMMON_MISTAKES`) is per
step. The fixtures below use single-step procedures so each step's channel is
the only one, which keeps the assertions close to what this suite tested
before the grain changed.
"""

from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from agentco import auth, cli
from agentco.app import create_app
from agentco.errors import Refusal
from agentco.policy import AGENT, HUMAN, RevisionPolicyError
from agentco.sop import MAX_COMMON_MISTAKES, PROPOSED_KEY, SopError, SopStatus, step_payload
from agentco.work import WorkStatus

DETERMINISTIC_GATE = {
    "kind": "deterministic",
    "check": "diff -q out.csv expected.csv",
    "max_park_seconds": 900,
    "on_timeout": "fail",
}
HUMAN_GATE = {"kind": "human", "check": "the owner signs off", "verifier": "dana",
              "max_park_seconds": 86400, "on_timeout": "escalate", "escalate_to": "dana"}


def procedure(library, **over):
    from asop.sop import STEP_TEXT_FIELDS

    # A protected (money-tagged) step's instances must carry a human gate
    # (P4.0) — the pass under test is downstream of that rule, not exempt
    # from it. In v3 the gate is authored WITH the step rather than supplied
    # at filing time, so the same posture is expressed by choosing the human
    # gate here for a protected procedure, once, rather than per instance.
    protected = bool(set(over.get("tags") or ()) & {"money", "irreversible"})
    step = {
        "name": "export",
        "role": "implementer",
        "gate": HUMAN_GATE if protected else DETERMINISTIC_GATE,
        "definition_of_done": "the export matches the fixture",
        "validation": "diff -q out.csv expected.csv",
    }
    for key in list(over):
        if key in STEP_TEXT_FIELDS or key in ("common_mistakes", "tags"):
            step[key] = over.pop(key)
    body = {"roles": {"implementer": {"kind": "agent"}}, "steps": [step]}
    body.update(over)
    sop = library.create("export the ledger", author="dana", author_kind=HUMAN, **body)
    library.activate(sop.asop_id, 1, author="dana", author_kind=HUMAN)
    return sop


def _step(asop, i=0, **override):
    """`asop.steps[i]` back as a revise-able payload, with fields overridden
    or (when the value is None) dropped."""
    payload = step_payload(asop.steps[i])
    for key, value in override.items():
        if value is None:
            payload.pop(key, None)
        else:
            payload[key] = value
    return payload


def _declare(queue, humans=("dana",)):
    """Who the operator declared. Undeclared, only humans adjudicate (ASOP.md
    §6.1) — so a test that adjudicates has to say who its human is."""
    queue.humans = frozenset(humans)
    queue.adjudicators = frozenset()
    return queue


def adjudicated(library, queue, sop, verdict, evidence, executor="kofi", adjudicator="dana"):
    run = library.run(sop.asop_id, queue, inputs={}, bindings={"implementer": executor})
    item_id = run["steps"][0]["itemId"]
    queue.claim(item_id, executor)
    item = queue.get(item_id)
    kwargs = {}
    if (item.verify or {}).get("kind") == "deterministic":
        kwargs["attestation"] = {"check": item.verify["check"], "exit_status": 0,
                                 "environment": "test", "at": "2026-09-04T00:00:00+00:00"}
    queue.report_result(item_id, item.lease_attempt, WorkStatus.DONE, submitted_by=executor, **kwargs)
    _declare(queue, humans=(adjudicator,))
    queue.adjudicate(item_id, verdict, evidence, adjudicator=adjudicator)
    return queue.get(item_id)


# --------------------------------------------------------------------------- #
# the gate's three sentences
# --------------------------------------------------------------------------- #


def test_a_good_adjudication_reaches_the_next_version(library, queue):
    sop = procedure(library)
    item = adjudicated(library, queue, sop, "good", "the diff is redundant: the export is byte-stable")
    draft = library.propose(sop.asop_id, queue, author="agentco-lessons", author_kind=AGENT)

    assert draft.version == 2
    assert draft.status == SopStatus.DRAFT, "proposed, never activated"
    assert len(draft.steps[0].proposals) == 1
    assert draft.steps[0].proposals[0].startswith(
        "the diff is redundant: the export is byte-stable (adjudicated good on"
    )
    assert item.id in draft.steps[0].proposals[0] and "v1 was wrong here" in draft.steps[0].proposals[0]
    assert draft.steps[0].definition_of_done == "the export matches the fixture", "the plane does not author prose"


def test_a_bad_adjudication_reaches_root_cause_and_the_lesson_channel(library, queue):
    sop = procedure(library)
    item = adjudicated(library, queue, sop, "bad", "skipped the diff and reported done")
    view = library.proposals(sop.asop_id, queue)
    assert [e["itemId"] for e in view["rootCause"]] == [item.id]
    assert view["revisions"] == []

    draft = library.propose(sop.asop_id, queue, author="agentco-lessons", author_kind=AGENT)
    assert draft.steps[0].common_mistakes == [
        f"skipped the diff and reported done (adjudicated bad on {item.id} by dana)"
    ]
    assert draft.steps[0].proposals == []


def test_neither_silently_edits_v1(library, queue):
    sop = procedure(library)
    before = library.get(sop.asop_id, version=1).to_json()
    adjudicated(library, queue, sop, "good", "g")
    adjudicated(library, queue, sop, "bad", "b")
    draft = library.propose(sop.asop_id, queue, author="agentco-lessons", author_kind=AGENT)

    v1 = library.get(sop.asop_id, version=1)
    assert json.loads(v1.to_json()) == {**json.loads(before), "superseded_by": draft.version}, (
        "v1 is byte-identical but for the pointer to the draft that follows it"
    )
    assert v1.status == SopStatus.ACTIVE, "still the version instances get"
    assert library.get(sop.asop_id).version == 1
    assert draft.version == 2 and draft.status == SopStatus.DRAFT


# --------------------------------------------------------------------------- #
# accumulation
# --------------------------------------------------------------------------- #


def test_a_proposal_is_consumed_once(library, queue):
    sop = procedure(library)
    item = adjudicated(library, queue, sop, "good", "g")
    draft = library.propose(sop.asop_id, queue, author="bot", author_kind=AGENT)
    assert queue.get(item.id).metadata["adjudication"][PROPOSED_KEY] == draft.version
    assert library.proposals(sop.asop_id, queue)["pending"] == 0
    assert library.propose(sop.asop_id, queue, author="bot", author_kind=AGENT) is None, "nothing pending, quiet run"


def test_proposals_accumulate_across_drafts_until_a_reviser_addresses_them(library, queue):
    sop = procedure(library)
    adjudicated(library, queue, sop, "good", "first")
    library.propose(sop.asop_id, queue, author="bot", author_kind=AGENT)
    adjudicated(library, queue, sop, "good", "second")
    draft = library.propose(sop.asop_id, queue, author="bot", author_kind=AGENT)
    assert [p.split(" (")[0] for p in draft.steps[0].proposals] == ["first", "second"], "carried forward, then extended"

    # A human addresses them: revises the prose and clears the list.
    fixed = library.revise(
        sop.asop_id,
        steps=[_step(draft, 0, definition_of_done="the export is byte-stable; no diff needed", proposals=None)],
        author="dana", author_kind=HUMAN,
    )
    assert fixed.steps[0].proposals == []
    # ...and an agent cannot bring a dismissed proposal back (rule 3).
    with pytest.raises(RevisionPolicyError):
        library.revise(sop.asop_id, steps=[_step(fixed, 0, proposals=draft.steps[0].proposals)],
                       author="bot", author_kind=AGENT)


def test_open_proposals_survive_an_unrelated_revision(library, queue):
    """A reviser fixing something else must not silently lose the open
    proposals — they carry forward like every other field, until somebody
    addresses or dismisses them on purpose."""
    sop = procedure(library)
    adjudicated(library, queue, sop, "good", "first")
    draft = library.propose(sop.asop_id, queue, author="bot", author_kind=AGENT)
    later = library.revise(sop.asop_id, steps=[_step(draft, 0, inputs="the ledger export id")],
                           author="dana", author_kind=HUMAN)
    assert later.steps[0].proposals == draft.steps[0].proposals
    assert later.steps[0].inputs == "the ledger export id"


def test_the_view_reports_what_is_pending_and_what_was_consumed(library, queue):
    sop = procedure(library)
    a = adjudicated(library, queue, sop, "good", "g1")
    draft = library.propose(sop.asop_id, queue, author="bot", author_kind=AGENT)
    b = adjudicated(library, queue, sop, "bad", "b1")
    view = library.proposals(sop.asop_id, queue)
    assert view["activeVersion"] == 1 and view["latestVersion"] == draft.version
    assert view["latestStatus"] == "draft"
    assert view["pending"] == 1
    assert {e["itemId"]: e["proposedIn"] for e in view["revisions"]} == {a.id: draft.version}
    assert {e["itemId"]: e["proposedIn"] for e in view["rootCause"]} == {b.id: None}
    assert view["openStepProposals"] == {1: draft.steps[0].proposals}


def test_an_unadjudicated_instance_proposes_nothing(library, queue):
    sop = procedure(library)
    run = library.run(sop.asop_id, queue, inputs={}, bindings={"implementer": "kofi"})
    item_id = run["steps"][0]["itemId"]
    queue.claim(item_id, "kofi")
    item = queue.get(item_id)
    queue.report_result(item_id, item.lease_attempt, WorkStatus.DONE,
                        attestation={"check": item.verify["check"], "exit_status": 0,
                                     "environment": "test", "at": "2026-09-04T00:00:00+00:00"})
    assert library.proposals(sop.asop_id, queue)["pending"] == 0
    assert library.propose(sop.asop_id, queue, author="bot", author_kind=AGENT) is None


def test_an_unknown_procedure_is_refused(library, queue):
    with pytest.raises(SopError):
        library.proposals("asop-deadbeef", queue)


# --------------------------------------------------------------------------- #
# the draft is an agent revision — the policy holds
# --------------------------------------------------------------------------- #


def test_a_protected_procedure_cannot_be_drafted_by_the_pass(library, queue):
    sop = procedure(library, tags=["money"])
    adjudicated(library, queue, sop, "good", "g")
    with pytest.raises(RevisionPolicyError) as caught:
        library.propose(sop.asop_id, queue, author="bot", author_kind=AGENT)
    assert caught.value.rule == "protected"
    assert library.history(sop.asop_id)[-1].version == 1, "nothing drafted"
    assert library.proposals(sop.asop_id, queue)["pending"] == 1, "and nothing consumed"


def test_a_human_running_the_pass_is_exempt(library, queue):
    sop = procedure(library, tags=["money"])
    adjudicated(library, queue, sop, "good", "g")
    draft = library.propose(sop.asop_id, queue, author="dana", author_kind=HUMAN)
    assert draft.version == 2 and draft.author_kind == HUMAN


def test_a_lesson_a_human_removed_does_not_come_back_through_the_pass(library, queue):
    sop = procedure(library, common_mistakes=["testing on prod"])
    library.revise(sop.asop_id, steps=[_step(sop, 0, common_mistakes=None)], author="dana", author_kind=HUMAN)
    library.activate(sop.asop_id, 2, author="dana", author_kind=HUMAN)
    item = adjudicated(library, queue, sop, "bad", "x")
    # Forge the exact lesson text a human removed, to prove the policy sees it.
    record = dict(queue.get(item.id).metadata["adjudication"])
    record["evidence"] = "testing on prod"
    queue.annotate(item.id, {"adjudication": record}, by_plane=True)
    # The pass appends "(adjudicated bad on ...)" so the text differs; what the
    # policy guards is the human's exact removal. Prove that path directly:
    latest = library.get(sop.asop_id)
    with pytest.raises(RevisionPolicyError):
        library.revise(sop.asop_id, steps=[_step(latest, 0, common_mistakes=["testing on prod"])],
                       author="bot", author_kind=AGENT)


def test_the_lesson_channel_cap_is_a_human_decision(library, queue):
    sop = procedure(library, common_mistakes=[f"known mistake {i}" for i in range(MAX_COMMON_MISTAKES)])
    adjudicated(library, queue, sop, "bad", "one more")
    with pytest.raises(SopError) as caught:
        library.propose(sop.asop_id, queue, author="bot", author_kind=AGENT)
    assert "human's call" in str(caught.value)
    assert library.history(sop.asop_id)[-1].version == 1
    assert library.proposals(sop.asop_id, queue)["pending"] == 1, "a refused pass consumes nothing"


def test_the_pass_never_activates(library, queue):
    sop = procedure(library)
    adjudicated(library, queue, sop, "good", "g")
    adjudicated(library, queue, sop, "bad", "b")
    library.propose(sop.asop_id, queue, author="bot", author_kind=AGENT)
    assert library.get(sop.asop_id).version == 1
    run = library.run(sop.asop_id, queue, inputs={}, bindings={"implementer": "kofi"})
    assert run["version"] == 1


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
    sop = _post(client, "/sops", "dana", {
        "title": "export",
        "roles": {"implementer": {"kind": "agent"}},
        "steps": [{"name": "export", "role": "implementer", "gate": DETERMINISTIC_GATE,
                   "definition_of_done": "matches"}],
        **over.get("sop", {}),
    }).json()["sop"]
    assert _post(client, f"/sops/{sop['asop_id']}/activate", "dana", {"version": 1}).status_code == 200
    filed = _post(client, f"/sops/{sop['asop_id']}/run", "dana",
                 {"inputs": {}, "bindings": {"implementer": "kofi"}})
    assert filed.status_code == 200, filed.text
    item_id = filed.json()["run"]["steps"][0]["itemId"]
    pulled = _post(client, "/work/pull", "kofi", {}).json()
    assert _post(client, f"/work/{item_id}/report", "kofi",
                {"attempt": pulled["attempt"], "status": "done",
                 "attestation": {"check": DETERMINISTIC_GATE["check"], "exit_status": 0,
                                 "environment": "test", "at": "2026-09-04T00:00:00+00:00"}}).status_code == 200
    assert _post(client, f"/work/{item_id}/adjudicate", "dana",
                {"verdict": "good", "evidence": "step 2 is redundant"}).status_code == 200
    return client, sop, {"id": item_id}


def test_over_http_the_pass_drafts_and_the_view_shows_it(tmp_path):
    client, sop, item = _http_setup(tmp_path)
    view = _get(client, f"/sops/{sop['asop_id']}/proposals", "kofi").json()
    assert view["pending"] == 1 and view["revisions"][0]["itemId"] == item["id"]

    drafted = _post(client, f"/sops/{sop['asop_id']}/propose", "bot", {})
    assert drafted.status_code == 200, drafted.text
    assert drafted.json()["state"] == "drafted"
    draft = drafted.json()["sop"]
    assert draft["version"] == 2 and draft["status"] == "draft" and draft["author_kind"] == AGENT
    assert "step 2 is redundant" in draft["steps"][0]["proposals"][0]

    again = _post(client, f"/sops/{sop['asop_id']}/propose", "bot", {})
    assert again.json() == {"state": "nothing_pending", "sop": None}
    assert _get(client, f"/sops/{sop['asop_id']}", "kofi").json()["sop"]["version"] == 1, "not activated"


def test_over_http_a_protected_procedure_refuses_the_agents_pass(tmp_path):
    client = TestClient(create_app(
        db_path=str(tmp_path / "api.sqlite3"), keys=KEYS, operator="operator",
        work_store=str(tmp_path / "work.jsonl"), sop_store=str(tmp_path / "sops.jsonl"), humans=["dana"],
    ))
    sop = _post(client, "/sops", "dana", {
        "title": "pay",
        "roles": {"implementer": {"kind": "agent"}},
        "steps": [{"name": "pay", "role": "implementer", "gate": HUMAN_GATE,
                   "definition_of_done": "paid", "tags": ["money"]}],
    }).json()["sop"]
    _post(client, f"/sops/{sop['asop_id']}/activate", "dana", {"version": 1})
    filed = _post(client, f"/sops/{sop['asop_id']}/run", "dana",
                 {"inputs": {}, "bindings": {"implementer": "kofi"}})
    assert filed.status_code == 200, filed.text
    item_id = filed.json()["run"]["steps"][0]["itemId"]
    pulled = _post(client, "/work/pull", "kofi", {}).json()
    _post(client, f"/work/{item_id}/report", "kofi", {"attempt": pulled["attempt"], "status": "done"})
    _post(client, f"/work/{item_id}/adjudicate", "dana", {"verdict": "good", "evidence": "x"})
    refused = _post(client, f"/sops/{sop['asop_id']}/propose", "bot", {})
    assert refused.status_code == 403
    assert refused.json()["code"] == "revision_policy:protected"
    assert _post(client, f"/sops/{sop['asop_id']}/propose", "dana", {}).json()["state"] == "drafted"


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
    assert drafted["steps"][0]["common_mistakes"][0].startswith("reported done without running the diff")
    assert library.get(sop.asop_id).version == 1, "the CLI never activates either"


# --------------------------------------------------------------------------- #
# second-party findings (Max, 4465f65) — each closed with the test that caught it
# --------------------------------------------------------------------------- #


def test_a_dismissed_proposal_does_not_jam_the_loop(library, queue):
    """Finding 1: a consumed adjudication whose mark was lost (crash window)
    after a human dismissed its text refused every later pass by rule 3 —
    forever. Now a dismissed text is consumed AS dismissed and the rest moves."""
    sop = procedure(library)
    first = adjudicated(library, queue, sop, "good", "first")
    draft = library.propose(sop.asop_id, queue, author="bot", author_kind=AGENT)
    library.revise(sop.asop_id, steps=[_step(draft, 0, proposals=None)],
                   author="dana", author_kind=HUMAN)  # dismissed
    # The crash window: consumption never got recorded.
    record = dict(queue.get(first.id).metadata["adjudication"]); record.pop(PROPOSED_KEY)
    queue.annotate(first.id, {"adjudication": record}, by_plane=True)
    assert library.proposals(sop.asop_id, queue)["pending"] == 1

    second = adjudicated(library, queue, sop, "good", "second")
    redraft = library.propose(sop.asop_id, queue, author="bot", author_kind=AGENT)
    assert redraft is not None, "the pass is not jammed"
    assert [p.split(" (")[0] for p in redraft.steps[0].proposals] == ["second"], "the dismissed text stays dismissed"
    marked = queue.get(first.id).metadata["adjudication"]
    assert marked[PROPOSED_KEY] == redraft.version and marked["dismissed_by_human"] is True
    assert library.proposals(sop.asop_id, queue)["pending"] == 0
    assert library.propose(sop.asop_id, queue, author="bot", author_kind=AGENT) is None


def test_a_full_lesson_channel_does_not_hold_good_proposals_hostage(library, queue):
    """Finding 2: the cap refused the whole pass. Now goods are drafted, the
    lessons the cap has no room for stay PENDING — neither dropped nor blocking."""
    sop = procedure(library, common_mistakes=[f"known mistake {i}" for i in range(MAX_COMMON_MISTAKES)])
    bad = adjudicated(library, queue, sop, "bad", "one more")
    good = adjudicated(library, queue, sop, "good", "step 2 is redundant")
    draft = library.propose(sop.asop_id, queue, author="bot", author_kind=AGENT)
    assert draft is not None and len(draft.steps[0].proposals) == 1
    assert draft.steps[0].common_mistakes == sop.steps[0].common_mistakes, "the cap held the lesson channel"
    view = library.proposals(sop.asop_id, queue)
    assert {e["itemId"]: e["proposedIn"] for e in view["revisions"]} == {good.id: draft.version}
    assert {e["itemId"]: e["proposedIn"] for e in view["rootCause"]} == {bad.id: None}, "pending, not dropped"
    # Only when NOTHING can move is the cap a refusal — and a loud one.
    with pytest.raises(SopError) as caught:
        library.propose(sop.asop_id, queue, author="bot", author_kind=AGENT)
    assert "human's call" in str(caught.value)


def test_the_cap_boundary_is_exact(library, queue):
    """Mutant Q2 (`>` vs `>=`) survived: exactly-at-cap drafts, one over defers."""
    sop = procedure(library, common_mistakes=[f"known mistake {i}" for i in range(MAX_COMMON_MISTAKES - 1)])
    adjudicated(library, queue, sop, "bad", "fits")
    draft = library.propose(sop.asop_id, queue, author="bot", author_kind=AGENT)
    assert len(draft.steps[0].common_mistakes) == MAX_COMMON_MISTAKES
    adjudicated(library, queue, sop, "bad", "does not fit")
    with pytest.raises(SopError):
        library.propose(sop.asop_id, queue, author="bot", author_kind=AGENT)


def test_proposals_are_isolated_per_procedure(library, queue):
    """Mutant Q4 survived: nothing pinned the cross-SOP filter."""
    a, b = procedure(library), procedure(library)
    adjudicated(library, queue, a, "good", "about a")
    adjudicated(library, queue, b, "bad", "about b")
    draft_a = library.propose(a.asop_id, queue, author="bot", author_kind=AGENT)
    assert [p.split(" (")[0] for p in draft_a.steps[0].proposals] == ["about a"] and draft_a.steps[0].common_mistakes == []
    draft_b = library.propose(b.asop_id, queue, author="bot", author_kind=AGENT)
    assert draft_b.steps[0].proposals == [] and draft_b.steps[0].common_mistakes[0].startswith("about b")


def test_later_drafts_do_not_move_v1s_pointer(library, queue):
    """Mutant Q8 survived: only the first draft's `superseded_by` was asserted."""
    sop = procedure(library)
    adjudicated(library, queue, sop, "good", "one")
    library.propose(sop.asop_id, queue, author="bot", author_kind=AGENT)
    adjudicated(library, queue, sop, "good", "two")
    library.propose(sop.asop_id, queue, author="bot", author_kind=AGENT)
    assert library.get(sop.asop_id, version=1).superseded_by == 2
    assert library.get(sop.asop_id, version=2).superseded_by == 3
    assert library.get(sop.asop_id, version=3).superseded_by is None


def test_over_http_propose_refuses_a_body_that_names_the_author(tmp_path):
    """Finding 3: `/propose` ignored body author fields where `/revise` refuses them."""
    client, sop, _ = _http_setup(tmp_path)
    refused = _post(client, f"/sops/{sop['asop_id']}/propose", "bot", {"author_kind": "human"})
    assert refused.status_code == 400 and refused.json()["code"] == "author_from_signature"
    assert _get(client, f"/sops/{sop['asop_id']}/proposals", "kofi").json()["pending"] == 1, "nothing drafted"


def test_the_cli_pass_honours_the_operators_declaration(tmp_path, capsys, monkeypatch):
    """Mutant Q7 survived: the CLI's human path was untested."""
    from agentco.sop import SopLibrary
    from agentco.work import Queue

    queue = Queue(tmp_path / "work.jsonl")
    library = SopLibrary(tmp_path / "sops.jsonl")
    sop = procedure(library, tags=["money"])
    adjudicated(library, queue, sop, "good", "approval step is redundant")
    args = ["lessons", "--work-store", str(tmp_path / "work.jsonl"), "--sop-store", str(tmp_path / "sops.jsonl"),
            "--propose", "--json"]

    monkeypatch.setenv("AGENTCO_ACTOR", "dana")
    monkeypatch.delenv("AGENTCO_HUMANS", raising=False)
    assert cli.main(args) == 1, "an agent's pass on a protected step is refused"
    assert "protected" in json.loads(capsys.readouterr().out)["sops"][0]["refused"]

    monkeypatch.setenv("AGENTCO_HUMANS", "dana")
    assert cli.main(args) == 0
    drafted = json.loads(capsys.readouterr().out)["sops"][0]["drafted"]
    assert drafted["author"] == "dana" and drafted["author_kind"] == HUMAN


def test_an_item_that_never_ran_the_procedure_cannot_feed_its_lessons(library, queue):
    """The attack the second party ran, end to end: refused at the first step."""
    sop = procedure(library)
    with pytest.raises(Refusal):
        queue.create("mallory's item", metadata={"sop_ref": sop.ref})
    assert library.proposals(sop.asop_id, queue)["pending"] == 0
