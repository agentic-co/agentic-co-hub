"""ASOPs as versioned objects that runs pin — ported to v3.

The tests that carry weight here are the ones defending the two properties
the whole idea rests on, now expressed in v3's terms (the ASOP is the
sequence, the run is a tree of steps):

  * **a template is not an instance** — an ASOP must never become claimable
    work: `create()` puts nothing in the queue, `run()` files a fresh TREE
    rather than turning the template itself into work, and a draft cannot be
    run;
  * **a pin is immutable** — if `revise()` or `activate()` reached back into
    a run already filed, every number computed from its outcomes would be
    attributing results to text that has since changed. Every bead a run
    files — the parent AND each step — must keep resolving to the exact
    version it ran under, forever.

"The body contract" below used to check flat SOP text fields; it now checks
the ASOP/Step shape instead, but the honesty rules it defends are unchanged:
partial is fine, a present-but-blank field is not, an empty list that claims
"no known failure modes" is not, and a malformed body is refused rather than
silently repaired.
"""

from __future__ import annotations

import pytest

from agentco.sop import (
    MAX_COMMON_MISTAKES,
    ASOP,
    SopContractError,
    SopError,
    SopLibrary,
    SopStatus,
)
from agentco.work import WorkStatus

# --------------------------------------------------------------------------- #
# fixtures — the running example from ASOP.md §1, trimmed to three steps.
# Copied from tests/test_asop_v3.py rather than invented fresh (per the
# migration handoff), since it is the shape every v3 test in this repo is
# already built against.
# --------------------------------------------------------------------------- #

DETERMINISTIC_GATE = {
    "kind": "deterministic",
    "check": "pytest -q",
    "max_park_seconds": 900,
    "on_timeout": "fail",
}
JUDGED_GATE = {
    "kind": "judged",
    "check": "a reviewer confirms the runbook step was actually followed",
    "max_park_seconds": 3600,
    "on_timeout": "escalate",
    "escalate_to": "release-owner",
}


def feature_dev_body(**over) -> dict:
    body = {
        "task_type": "feature",
        "purpose": "take a specified feature from requirement to verified code",
        "trigger": "a feature bead exists with a written requirement and an owner",
        "inputs": [
            {"name": "requirement", "description": "the requirement the feature answers"},
            {"name": "repo", "description": "the repository the feature lands in"},
        ],
        "roles": {"implementer": {"kind": "agent"}, "validator": {"kind": "agent"}},
        "constraints": [{"distinct": ["implementer", "validator"]}],
        "steps": [
            {
                "name": "implement",
                "role": "implementer",
                "purpose": "write the code the requirement describes",
                "definition_of_done": "the feature exists behind its flag",
                "gate": DETERMINISTIC_GATE,
            },
            {
                "name": "run-tests",
                "role": "implementer",
                "purpose": "prove the suite is green",
                "validation": "the suite exits 0 on a clean checkout",
                "gate": DETERMINISTIC_GATE,
            },
            {
                "name": "validate",
                "role": "validator",
                "purpose": "confirm the feature satisfies the requirement as written",
                "validation": "a traceability table maps every criterion to a test id",
                "gate": JUDGED_GATE,
            },
        ],
    }
    body.update(over)
    return body


def an_asop(library, title="develop a feature", **over) -> ASOP:
    return library.create(title, **feature_dev_body(**over))


def an_active_asop(library, **over) -> ASOP:
    asop = an_asop(library, **over)
    return library.activate(asop.asop_id, asop.version)


RUN_INPUTS = {"requirement": "REQ-1", "repo": "org/repo"}
RUN_BINDINGS = {"implementer": "alice", "validator": "bob"}


def a_run(library, queue, asop, **over):
    kwargs = {"inputs": RUN_INPUTS, "bindings": RUN_BINDINGS}
    kwargs.update(over)
    return library.run(asop.asop_id, queue, **kwargs)


def _finish(queue, item_id, actor="alice", result="did it"):
    """Finish a DETERMINISTIC-gated (or ungated) item as a normal completion."""
    queue.claim(item_id, actor, capabilities=["verify"])
    item = queue.get(item_id)
    attestation = None
    if item.is_gated and (item.verify or {}).get("kind") == "deterministic":
        attestation = {
            "check": item.verify["check"], "exit_status": 0,
            "environment": "test", "at": "2026-09-04T00:00:00+00:00",
        }
    queue.report_result(item_id, item.lease_attempt, WorkStatus.DONE, result=result,
                        attestation=attestation, submitted_by=actor)


# --------------------------------------------------------------------------- #
# Template is not instance
# --------------------------------------------------------------------------- #


def test_creating_an_asop_puts_nothing_in_the_work_queue(library, queue):
    """The load-bearing separation. If an ASOP entered the queue it would be
    claimable, and a template that can be completed is a bug."""
    an_asop(library)
    assert queue.list() == []


def test_running_an_active_asop_files_a_tree_that_pins_the_version(library, queue):
    asop = an_active_asop(library)
    run = a_run(library, queue, asop)

    parent = queue.get(run["runId"])
    assert parent.metadata["sop_ref"] == {"asop_id": asop.asop_id, "version": 1}
    assert parent.status == WorkStatus.PENDING
    for step in run["steps"]:
        child = queue.get(step["itemId"])
        assert child.metadata["sop_ref"]["asop_id"] == asop.asop_id
        assert child.metadata["sop_ref"]["version"] == 1
    assert len(queue.list()) == 4  # one parent, three steps


def test_a_draft_cannot_be_run(library, queue):
    """Generating work from an unactivated procedure hands somebody a
    half-written instruction with the authority of a published one."""
    asop = an_asop(library)
    assert asop.status == SopStatus.DRAFT
    with pytest.raises(SopError) as exc:
        a_run(library, queue, asop)
    assert "draft" in str(exc.value)
    assert queue.list() == []


def test_step_beads_are_ordinary_work_items(library, queue):
    """Nothing about being ASOP-derived changes how the queue treats a step."""
    asop = an_active_asop(library)
    run = a_run(library, queue, asop)
    first = run["steps"][0]["itemId"]

    claimed = queue.claim(first, "alice", capabilities=["verify"])
    assert claimed is not None
    _finish(queue, first)
    assert queue.get(first).status == WorkStatus.DONE


# --------------------------------------------------------------------------- #
# Versions are immutable, and pins do not move
# --------------------------------------------------------------------------- #


def test_revising_writes_a_new_version_and_leaves_the_old_one_intact(library):
    asop = an_asop(library)
    revised = library.revise(asop.asop_id, purpose="take a feature further, then announce it",
                             author="carol", author_kind="human")

    assert revised.version == 2
    original = library.get(asop.asop_id, version=1)
    assert original.purpose == feature_dev_body()["purpose"], "v1 must not change"


def test_a_revision_does_not_reach_back_into_a_running_run(library, queue):
    """THE test. If a pin moved, every outcome would be attributed to text
    that has since changed, and the evaluation would be fiction. This must
    hold for the parent AND for every step it filed."""
    asop = an_active_asop(library)
    run = a_run(library, queue, asop)

    library.revise(asop.asop_id, purpose="a completely different procedure",
                   author="carol", author_kind="human")
    library.activate(asop.asop_id, 2, author="carol", author_kind="human")

    assert queue.get(run["runId"]).metadata["sop_ref"]["version"] == 1
    for step in run["steps"]:
        assert queue.get(step["itemId"]).metadata["sop_ref"]["version"] == 1


def test_a_superseded_version_stays_resolvable_forever(library):
    """Runs pinned to it must remain readable, or improving a procedure would
    make its own history unreadable."""
    asop = an_active_asop(library)
    library.revise(asop.asop_id, steps=feature_dev_body()["steps"], author="carol", author_kind="human")
    library.activate(asop.asop_id, 2, author="carol", author_kind="human")

    old = library.get(asop.asop_id, version=1)
    assert old is not None
    assert old.status == SopStatus.SUPERSEDED
    assert old.purpose == feature_dev_body()["purpose"]


def test_unset_fields_carry_forward_across_a_revision(library):
    """A change to one line must not silently blank the rest — including the
    whole `steps` list, which v2 never had to worry about carrying."""
    asop = an_asop(library)
    revised = library.revise(asop.asop_id, purpose="a sharper purpose", author="carol", author_kind="human")
    assert [s.name for s in revised.steps] == ["implement", "run-tests", "validate"]
    assert revised.trigger == feature_dev_body()["trigger"]


def test_get_without_a_version_returns_the_active_one(library):
    asop = an_asop(library)
    library.activate(asop.asop_id, 1)
    library.revise(asop.asop_id, purpose="x", author="carol", author_kind="human")
    assert library.get(asop.asop_id).version == 1, "v2 is a draft until activated"
    library.activate(asop.asop_id, 2, author="carol", author_kind="human")
    assert library.get(asop.asop_id).version == 2


def test_drafting_a_revision_does_not_take_the_live_procedure_out_of_service(library, queue):
    """Regression. `revise()` must not flip the ACTIVE version to SUPERSEDED,
    so merely DRAFTING an improvement leaves the ASOP with no active version
    and the next `run()` fails — with nothing having been deliberately
    changed. Writing a draft must be a safe, invisible act."""
    asop = an_active_asop(library)
    library.revise(asop.asop_id, purpose="x", author="carol", author_kind="human")

    assert library.get(asop.asop_id).version == 1, "v1 stays live while v2 is a draft"
    run = a_run(library, queue, asop)
    assert queue.get(run["runId"]).metadata["sop_ref"]["version"] == 1


def test_a_superseded_by_marker_is_not_a_deactivation(library):
    """The two facts are separate: 'a later version exists' and 'this one is
    no longer the one to use'. Only `activate()` asserts the second."""
    asop = an_active_asop(library)
    library.revise(asop.asop_id, purpose="x", author="carol", author_kind="human")
    v1 = library.get(asop.asop_id, version=1)
    assert v1.superseded_by == 2
    assert v1.status == SopStatus.ACTIVE


def test_the_error_names_the_right_problem_when_only_drafts_exist(library, queue):
    """A message that blames the wrong thing sends the reader looking in the
    wrong place — 'no such ASOP' when it exists but is unactivated is worse
    than no message."""
    asop = an_asop(library)
    with pytest.raises(SopError) as exc:
        a_run(library, queue, asop)
    assert "no active version" in str(exc.value)
    assert "Activate it first" in str(exc.value)


def test_only_one_version_is_active_at_a_time(library):
    asop = an_active_asop(library)
    library.revise(asop.asop_id, purpose="x", author="carol", author_kind="human")
    library.activate(asop.asop_id, 2, author="carol", author_kind="human")
    active = [a for a in library.history(asop.asop_id) if a.status == SopStatus.ACTIVE]
    assert [a.version for a in active] == [2]


def test_revising_something_that_does_not_exist_is_refused(library):
    """Creating v1 under a caller's belief they were editing a procedure
    people already follow is worse than an error."""
    with pytest.raises(SopError):
        library.revise("asop-nope", purpose="x")


# --------------------------------------------------------------------------- #
# The body contract, in the ASOP/Step shape — partial is fine, dishonest is not
# --------------------------------------------------------------------------- #


def test_a_partial_asop_is_legal(library):
    """A step is filled in as the work is understood. Demanding every text
    field up front means it is skipped when it is cheapest to start — the
    only things a step must carry are its name, its role, its gate, and at
    least one line of text."""
    asop = library.create(
        "triage",
        roles={"triager": {"kind": "agent"}},
        steps=[{"name": "decide", "role": "triager", "purpose": "decide whether it is urgent",
                "gate": DETERMINISTIC_GATE}],
    )
    step = asop.steps[0]
    assert step.purpose == "decide whether it is urgent"
    assert step.entry_check is None
    assert step.definition_of_done is None
    assert step.validation is None
    assert step.write_back is None


def test_an_asop_with_no_steps_is_refused(library):
    """An ASOP with no steps is a title. Nothing about the work is answered."""
    with pytest.raises(SopContractError):
        library.create("nothing")


def test_a_blank_asop_field_is_refused(library):
    """It claims to answer a question it does not answer."""
    with pytest.raises(SopContractError) as exc:
        an_asop(library, purpose="   ")
    assert "does not answer" in str(exc.value)


def test_a_blank_step_field_is_refused(library):
    body = feature_dev_body()
    body["steps"][0]["purpose"] = "   "
    with pytest.raises(SopContractError) as exc:
        library.create("blank step", **body)
    assert "does not answer" in str(exc.value)


def test_an_unknown_asop_field_is_refused_and_lists_the_allowed_ones(library):
    body = feature_dev_body()
    body["next_milestone"] = "beta"
    with pytest.raises(SopContractError) as exc:
        library.create("triage", **body)
    message = str(exc.value)
    assert "next_milestone" in message
    assert "steps" in message


def test_an_unknown_step_field_is_refused(library):
    body = feature_dev_body()
    body["steps"][0]["executor"] = "alice"
    with pytest.raises(SopContractError) as exc:
        library.create("triage", **body)
    assert "executor" in str(exc.value)


def test_an_empty_common_mistakes_list_is_refused(library):
    """An empty list claims this step has no known failure modes, which is
    the one claim a handoff should never make silently."""
    body = feature_dev_body()
    body["steps"][0]["common_mistakes"] = []
    with pytest.raises(SopContractError) as exc:
        library.create("triage", **body)
    assert "empty" in str(exc.value)


def test_common_mistakes_is_capped(library):
    body = feature_dev_body()
    body["steps"][0]["common_mistakes"] = ["a", "b", "c", "d"]
    with pytest.raises(SopContractError) as exc:
        library.create("triage", **body)
    assert f"cap is {MAX_COMMON_MISTAKES}" in str(exc.value)


def test_a_malformed_step_is_refused_rather_than_repaired(library):
    """A repaired body means the caller believes they wrote one thing and
    the store holds another, and it surfaces at handoff to whoever can
    least tell."""
    body = feature_dev_body()
    body["steps"][0]["common_mistakes"] = "not a list"
    with pytest.raises(SopContractError):
        library.create("triage", **body)


def test_a_step_declares_a_role_the_asop_does_not_have_is_refused(library):
    body = feature_dev_body()
    body["steps"][0]["role"] = "nobody"
    with pytest.raises(SopContractError) as exc:
        library.create("triage", **body)
    assert "nobody" in str(exc.value)


# --------------------------------------------------------------------------- #
# Evaluation — the point of all the above
# --------------------------------------------------------------------------- #


def _pin(asop) -> dict:
    return dict(asop.ref)


def test_outcomes_are_grouped_by_version(library, queue):
    asop = an_active_asop(library)
    a = queue.create("a run", by_plane=True, metadata={"sop_ref": _pin(asop)})
    b = queue.create("a run", by_plane=True, metadata={"sop_ref": _pin(asop)})
    _finish(queue, a.id)
    queue.report_result(b.id, queue.claim(b.id, "worker").lease_attempt, WorkStatus.FAILED)

    library.revise(asop.asop_id, purpose="x", author="carol", author_kind="human")
    library.activate(asop.asop_id, 2, author="carol", author_kind="human")
    c = queue.create("a run", by_plane=True, metadata={"sop_ref": {"asop_id": asop.asop_id, "version": 2}})
    _finish(queue, c.id)

    rows = {r["version"]: r for r in library.outcomes_by_version(asop.asop_id, queue)}
    assert (rows[1]["done"], rows[1]["failed"]) == (1, 1)
    assert (rows[2]["done"], rows[2]["failed"]) == (1, 0)
    assert rows[1]["successRate"] == 0.5
    assert rows[2]["successRate"] == 1.0


def test_a_version_with_no_finished_runs_reports_none_not_zero(library, queue):
    """An unreported number must never read as a measured zero — a brand-new
    version would otherwise look like a 0% success rate."""
    asop = an_active_asop(library)
    queue.create("a run", by_plane=True, metadata={"sop_ref": _pin(asop)})

    row = library.outcomes_by_version(asop.asop_id, queue)[0]
    assert row["runs"] == 1
    assert row["inFlight"] == 1
    assert row["successRate"] is None


def test_in_flight_work_counts_as_neither_outcome(library, queue):
    """Counting it either way would make the rate move as work merely starts."""
    asop = an_active_asop(library)
    done = queue.create("a run", by_plane=True, metadata={"sop_ref": _pin(asop)})
    _finish(queue, done.id)
    queue.create("a run", by_plane=True, metadata={"sop_ref": _pin(asop)})

    row = library.outcomes_by_version(asop.asop_id, queue)[0]
    assert (row["done"], row["failed"], row["inFlight"]) == (1, 0, 1)
    assert row["successRate"] == 1.0


def test_a_gated_run_awaiting_its_verdict_is_its_own_column(library, queue):
    """Not a success, not a failure, and not the same as untouched work. It is
    the difference between "this procedure is slow to verify" and "nobody is
    running it" — two findings that prompt opposite responses."""
    asop = an_active_asop(library)
    item = queue.create("gated run", by_plane=True, metadata={"sop_ref": _pin(asop)}, verify=JUDGED_GATE)
    queue.claim(item.id, "worker")
    queue.report_result(item.id, queue.get(item.id).lease_attempt, WorkStatus.DONE)

    row = library.outcomes_by_version(asop.asop_id, queue)[0]
    assert (row["awaitingVerify"], row["inFlight"]) == (1, 0)
    assert (row["done"], row["failed"]) == (0, 0)
    assert row["successRate"] is None, "an open gate is not a settled outcome"
    assert row["unresolved"] == 1


def test_a_failed_gate_is_reported_and_kept_out_of_the_rate(library, queue):
    """The most informative number here, and the one most easily buried.
    `verifyFailed` says the work claimed the step's own definition of done and
    the gate disagreed. It still stays out of `successRate`, for the same
    reason a fractional credit was rejected: a re-verify can overturn it."""
    asop = an_active_asop(library)
    clean = queue.create("clean", metadata={"sop_ref": _pin(asop)}, by_plane=True)
    _finish(queue, clean.id)

    gated = queue.create("gated", metadata={"sop_ref": _pin(asop)}, verify=DETERMINISTIC_GATE, by_plane=True)
    claimed = queue.claim(gated.id, "worker")
    queue.report_result(
        gated.id, claimed.lease_attempt, WorkStatus.DONE,
        attestation={"check": "pytest -q", "exit_status": 1, "environment": "ci", "at": "2026-09-01T12:00:00+00:00"},
    )

    row = library.outcomes_by_version(asop.asop_id, queue)[0]
    assert row["verifyFailed"] == 1
    assert (row["done"], row["failed"]) == (1, 0)
    assert row["successRate"] == 1.0, "the rate covers settled outcomes only"
    assert row["unresolved"] == 1, "and the row says how much is not settled"


def test_a_passing_gate_lands_in_done_like_any_other_completion(library, queue):
    asop = an_active_asop(library)
    gated = queue.create("gated", by_plane=True, metadata={"sop_ref": _pin(asop)}, verify=DETERMINISTIC_GATE)
    _finish(queue, gated.id)

    row = library.outcomes_by_version(asop.asop_id, queue)[0]
    assert (row["done"], row["awaitingVerify"], row["verifyFailed"]) == (1, 0, 0)
    assert row["successRate"] == 1.0


def test_every_version_appears_even_with_no_runs(library, queue):
    """A version that produced nothing is a fact worth seeing — it usually
    means it was activated and then immediately replaced."""
    asop = an_active_asop(library)
    library.revise(asop.asop_id, purpose="x", author="carol", author_kind="human")
    rows = library.outcomes_by_version(asop.asop_id, queue)
    assert [r["version"] for r in rows] == [1, 2]
    assert all(r["runs"] == 0 for r in rows)


def test_work_items_with_no_sop_ref_are_ignored(library, queue):
    asop = an_active_asop(library)
    queue.create("unrelated work")
    queue.create("a run", by_plane=True, metadata={"sop_ref": _pin(asop)})
    assert library.outcomes_by_version(asop.asop_id, queue)[0]["runs"] == 1


# --------------------------------------------------------------------------- #
# Drift — reported, never migrated
# --------------------------------------------------------------------------- #


def test_an_in_flight_run_on_an_old_version_is_reported_as_drifted(library, queue):
    asop = an_active_asop(library)
    run = a_run(library, queue, asop)
    library.revise(asop.asop_id, purpose="x", author="carol", author_kind="human")
    library.activate(asop.asop_id, 2, author="carol", author_kind="human")

    drift = library.drifted(asop.asop_id, queue)
    assert len(drift) == 1
    assert drift[0]["runId"] == run["runId"]
    assert (drift[0]["pinnedVersion"], drift[0]["activeVersion"]) == (1, 2)


def test_drift_never_migrates_the_run(library, queue):
    """Re-pointing running work at a newer procedure changes the job under
    whoever is doing it, and they would have no way to know."""
    asop = an_active_asop(library)
    run = a_run(library, queue, asop)
    library.revise(asop.asop_id, purpose="x", author="carol", author_kind="human")
    library.activate(asop.asop_id, 2, author="carol", author_kind="human")

    library.drifted(asop.asop_id, queue)
    assert queue.get(run["runId"]).metadata["sop_ref"]["version"] == 1


def test_finished_runs_are_not_drift(library, queue):
    """Drift is a question about work still in progress. A completed run ran
    the procedure that was current when it ran, and that is the correct
    record."""
    asop = an_active_asop(library)
    item = queue.create("a run", by_plane=True, metadata={"sop_ref": _pin(asop)})
    _finish(queue, item.id)
    library.revise(asop.asop_id, purpose="x", author="carol", author_kind="human")
    library.activate(asop.asop_id, 2, author="carol", author_kind="human")
    assert library.drifted(asop.asop_id, queue) == []


def test_no_drift_when_the_pin_is_current(library, queue):
    asop = an_active_asop(library)
    a_run(library, queue, asop)
    assert library.drifted(asop.asop_id, queue) == []


# --------------------------------------------------------------------------- #
# legacy rows stay readable and pinned-resolvable (§2.1)
# --------------------------------------------------------------------------- #


@pytest.fixture()
def legacy_row(tmp_path):
    """A v2 row written straight into a JSONL store, as an existing install
    has it.

    JSONL only, and deliberately: this is a statement about a file whose
    lines predate the v3 record. The SQLite/Postgres equivalent is the
    numbered migration, and it is tested where migrations are —
    `tests/test_sqlstore.py`.
    """
    import json

    path = tmp_path / "sops.jsonl"
    path.write_text(json.dumps({
        "sop_id": "sop-legacy1",
        "version": 1,
        "title": "incident response",
        "status": "active",
        "purpose": "restore a service after an alert",
        "definition_of_done": "the alert is closed",
        "validation": "the dashboard is green for ten minutes",
        "common_mistakes": ["closed the alert before the cause was written down"],
        "executor": "agent",
        "tags": [],
        "proposals": [],
        "author": "carol",
        "author_kind": "human",
    }) + "\n")
    return SopLibrary(path), "sop-legacy1"


def test_a_legacy_sop_row_reads_back_as_a_one_step_asop(legacy_row):
    library, sop_id = legacy_row
    asop = library.get(sop_id, version=1)
    assert isinstance(asop, ASOP)
    assert asop.asop_id == sop_id
    assert len(asop.steps) == 1
    assert asop.steps[0].definition_of_done == "the alert is closed"
    # A v2 record carried no gate. The migration will not invent an
    # agent-closable one: it fails closed to a human gate, which a human can
    # revise.
    assert asop.steps[0].gate["kind"] == "human"
    assert asop.status is SopStatus.ACTIVE
