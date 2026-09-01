"""SOPs as versioned objects that work items pin.

The tests that carry weight here are the ones defending the two properties the
whole idea rests on:

  * **a template is not an instance** — an SOP must never become claimable
    work, or the template gets completed and the procedure is `done`;
  * **a pin is immutable** — if revising an SOP reached back into running
    instances, every number computed from their outcomes would be attributing
    results to text that has since changed.
"""

from __future__ import annotations

import pytest

from agentco.sop import (
    MAX_COMMON_MISTAKES,
    SOP,
    SopContractError,
    SopError,
    SopLibrary,
    SopStatus,
)
from agentco.work import Queue, WorkStatus


def a_sop(library, **over):
    body = {
        "purpose": "restore a service after an alert",
        "definition_of_done": "the alert is closed and the cause is written down",
    }
    body.update(over)
    return library.create("incident response", **body)


# --------------------------------------------------------------------------- #
# Template is not instance
# --------------------------------------------------------------------------- #


def test_creating_an_sop_puts_nothing_in_the_work_queue(library, queue):
    """The load-bearing separation. If an SOP entered the queue it would be
    claimable, and a template that can be completed is a bug."""
    a_sop(library)
    assert queue.list() == []


def test_instantiating_creates_a_work_item_that_pins_the_version(library, queue):
    sop = a_sop(library)
    library.activate(sop.sop_id, 1)

    item = library.instantiate(sop.sop_id, queue)
    assert item.metadata["sop_ref"] == {"sop_id": sop.sop_id, "version": 1}
    assert item.status == WorkStatus.PENDING
    assert len(queue.list()) == 1


def test_a_draft_cannot_be_instantiated(library, queue):
    """Generating work from an unactivated procedure hands somebody a
    half-written instruction with the authority of a published one."""
    sop = a_sop(library)
    assert sop.status == SopStatus.DRAFT
    with pytest.raises(SopError) as exc:
        library.instantiate(sop.sop_id, queue)
    assert "draft" in str(exc.value)


def test_instances_are_ordinary_work_items(library, queue):
    """Nothing about being SOP-derived changes how the queue treats it."""
    sop = a_sop(library)
    library.activate(sop.sop_id, 1)
    item = library.instantiate(sop.sop_id, queue)

    claimed = queue.claim(item.id, "worker-a")
    assert claimed is not None
    queue.report_result(item.id, claimed.lease_attempt, WorkStatus.DONE)
    assert queue.get(item.id).status == WorkStatus.DONE


# --------------------------------------------------------------------------- #
# Versions are immutable, and pins do not move
# --------------------------------------------------------------------------- #


def test_revising_writes_a_new_version_and_leaves_the_old_one_intact(library):
    sop = a_sop(library)
    revised = library.revise(sop.sop_id, purpose="restore a service, then post a summary")

    assert revised.version == 2
    original = library.get(sop.sop_id, version=1)
    assert original.purpose == "restore a service after an alert", "v1 must not change"


def test_a_revision_does_not_reach_back_into_a_running_instance(library, queue):
    """THE test. If the pin moved, every outcome would be attributed to text
    that has since changed, and the evaluation would be fiction."""
    sop = a_sop(library)
    library.activate(sop.sop_id, 1)
    item = library.instantiate(sop.sop_id, queue)

    library.revise(sop.sop_id, purpose="a completely different procedure")
    library.activate(sop.sop_id, 2)

    assert queue.get(item.id).metadata["sop_ref"]["version"] == 1


def test_a_superseded_version_stays_resolvable_forever(library):
    """Instances pinned to it must remain readable, or improving a procedure
    would make its own history unreadable."""
    sop = a_sop(library)
    library.activate(sop.sop_id, 1)
    library.revise(sop.sop_id, inputs="the alert payload")
    library.activate(sop.sop_id, 2)

    old = library.get(sop.sop_id, version=1)
    assert old is not None
    assert old.status == SopStatus.SUPERSEDED
    assert old.purpose == "restore a service after an alert"


def test_unset_fields_carry_forward_across_a_revision(library):
    """A change to one line must not silently blank the other four."""
    sop = a_sop(library, inputs="the alert payload")
    revised = library.revise(sop.sop_id, purpose="a sharper purpose")
    assert revised.inputs == "the alert payload"
    assert revised.definition_of_done == "the alert is closed and the cause is written down"


def test_get_without_a_version_returns_the_active_one(library):
    sop = a_sop(library)
    library.activate(sop.sop_id, 1)
    library.revise(sop.sop_id, inputs="x")
    assert library.get(sop.sop_id).version == 1, "v2 is a draft until activated"
    library.activate(sop.sop_id, 2)
    assert library.get(sop.sop_id).version == 2


def test_drafting_a_revision_does_not_take_the_live_procedure_out_of_service(library, queue):
    """Regression. `revise()` used to flip the ACTIVE version to SUPERSEDED, so
    merely DRAFTING an improvement left the SOP with no active version and the
    next `instantiate()` failed — with nothing having been deliberately changed.
    Writing a draft must be a safe, invisible act."""
    sop = a_sop(library)
    library.activate(sop.sop_id, 1)
    library.revise(sop.sop_id, inputs="the alert payload")

    assert library.get(sop.sop_id).version == 1, "v1 stays live while v2 is a draft"
    assert library.instantiate(sop.sop_id, queue).metadata["sop_ref"]["version"] == 1


def test_a_superseded_by_marker_is_not_a_deactivation(library):
    """The two facts are separate: 'a later version exists' and 'this one is no
    longer the one to use'. Only `activate()` asserts the second."""
    sop = a_sop(library)
    library.activate(sop.sop_id, 1)
    library.revise(sop.sop_id, inputs="x")
    v1 = library.get(sop.sop_id, version=1)
    assert v1.superseded_by == 2
    assert v1.status == SopStatus.ACTIVE


def test_the_error_names_the_right_problem_when_only_drafts_exist(library, queue):
    """A message that blames the wrong thing sends the reader looking in the
    wrong place — 'no such SOP' when it exists but is unactivated is worse than
    no message."""
    sop = a_sop(library)
    with pytest.raises(SopError) as exc:
        library.instantiate(sop.sop_id, queue)
    assert "no active version" in str(exc.value)
    assert "Activate it first" in str(exc.value)


def test_only_one_version_is_active_at_a_time(library):
    sop = a_sop(library)
    library.activate(sop.sop_id, 1)
    library.revise(sop.sop_id, inputs="x")
    library.activate(sop.sop_id, 2)
    active = [s for s in library.history(sop.sop_id) if s.status == SopStatus.ACTIVE]
    assert [s.version for s in active] == [2]


def test_revising_something_that_does_not_exist_is_refused(library):
    """Creating v1 under a caller's belief they were editing a procedure people
    already follow is worse than an error."""
    with pytest.raises(SopError):
        library.revise("sop-nope", purpose="x")


# --------------------------------------------------------------------------- #
# The body contract — partial is fine, dishonest is not
# --------------------------------------------------------------------------- #


def test_a_partial_sop_is_legal(library):
    """An SOP is filled in as the work is understood. Demanding all five fields
    up front means it is skipped when it is cheapest to start."""
    sop = library.create("triage", purpose="decide whether it is urgent")
    assert sop.purpose and sop.trigger is None


def test_an_empty_sop_is_refused(library):
    with pytest.raises(SopContractError):
        library.create("nothing")


def test_a_present_but_blank_field_is_refused(library):
    """It claims to answer a question it does not answer."""
    with pytest.raises(SopContractError) as exc:
        library.create("triage", purpose="   ")
    assert "does not answer" in str(exc.value)


def test_an_unknown_field_is_refused_and_the_message_explains_steps(library):
    with pytest.raises(SopContractError) as exc:
        library.create("triage", steps="do the thing")
    assert "no 'steps' field" in str(exc.value)


def test_an_empty_common_mistakes_list_is_refused(library):
    """An empty list claims this work has no known failure modes, which is the
    one claim a handoff should never make silently."""
    with pytest.raises(SopContractError) as exc:
        library.create("triage", purpose="x", common_mistakes=[])
    assert "never make silently" in str(exc.value)


def test_common_mistakes_is_capped(library):
    with pytest.raises(SopContractError) as exc:
        library.create(
            "triage", purpose="x", common_mistakes=["a", "b", "c", "d"]
        )
    assert f"cap is {MAX_COMMON_MISTAKES}" in str(exc.value)


def test_a_malformed_sop_is_refused_rather_than_repaired(library):
    """A repaired SOP means the caller believes they wrote one thing and the
    store holds another, and it surfaces at handoff to whoever can least tell."""
    with pytest.raises(SopContractError):
        library.create("triage", purpose="x", common_mistakes="not a list")


# --------------------------------------------------------------------------- #
# Evaluation — the point of all the above
# --------------------------------------------------------------------------- #


def finish(queue, item, status):
    claimed = queue.claim(item.id, "worker")
    return queue.report_result(item.id, claimed.lease_attempt, status)


def test_outcomes_are_grouped_by_version(library, queue):
    sop = a_sop(library)
    library.activate(sop.sop_id, 1)
    finish(queue, library.instantiate(sop.sop_id, queue), WorkStatus.DONE)
    finish(queue, library.instantiate(sop.sop_id, queue), WorkStatus.FAILED)

    library.revise(sop.sop_id, inputs="the alert payload")
    library.activate(sop.sop_id, 2)
    finish(queue, library.instantiate(sop.sop_id, queue), WorkStatus.DONE)

    rows = {r["version"]: r for r in library.outcomes_by_version(sop.sop_id, queue)}
    assert (rows[1]["done"], rows[1]["failed"]) == (1, 1)
    assert (rows[2]["done"], rows[2]["failed"]) == (1, 0)
    assert rows[1]["successRate"] == 0.5
    assert rows[2]["successRate"] == 1.0


def test_a_version_with_no_finished_instances_reports_none_not_zero(library, queue):
    """An unreported number must never read as a measured zero — a brand-new
    version would otherwise look like a 0% success rate."""
    sop = a_sop(library)
    library.activate(sop.sop_id, 1)
    library.instantiate(sop.sop_id, queue)

    row = library.outcomes_by_version(sop.sop_id, queue)[0]
    assert row["instances"] == 1
    assert row["inFlight"] == 1
    assert row["successRate"] is None


def test_in_flight_work_counts_as_neither_outcome(library, queue):
    """Counting it either way would make the rate move as work merely starts."""
    sop = a_sop(library)
    library.activate(sop.sop_id, 1)
    finish(queue, library.instantiate(sop.sop_id, queue), WorkStatus.DONE)
    library.instantiate(sop.sop_id, queue)

    row = library.outcomes_by_version(sop.sop_id, queue)[0]
    assert (row["done"], row["failed"], row["inFlight"]) == (1, 0, 1)
    assert row["successRate"] == 1.0


def test_every_version_appears_even_with_no_instances(library, queue):
    """A version that produced nothing is a fact worth seeing — it usually means
    it was activated and then immediately replaced."""
    sop = a_sop(library)
    library.revise(sop.sop_id, inputs="x")
    rows = library.outcomes_by_version(sop.sop_id, queue)
    assert [r["version"] for r in rows] == [1, 2]
    assert all(r["instances"] == 0 for r in rows)


def test_work_items_with_no_sop_ref_are_ignored(library, queue):
    sop = a_sop(library)
    library.activate(sop.sop_id, 1)
    queue.create("unrelated work")
    library.instantiate(sop.sop_id, queue)
    assert library.outcomes_by_version(sop.sop_id, queue)[0]["instances"] == 1


# --------------------------------------------------------------------------- #
# Drift — reported, never migrated
# --------------------------------------------------------------------------- #


def test_an_in_flight_instance_on_an_old_version_is_reported_as_drifted(library, queue):
    sop = a_sop(library)
    library.activate(sop.sop_id, 1)
    item = library.instantiate(sop.sop_id, queue)
    library.revise(sop.sop_id, inputs="x")
    library.activate(sop.sop_id, 2)

    drift = library.drifted(sop.sop_id, queue)
    assert len(drift) == 1
    assert drift[0]["itemId"] == item.id
    assert (drift[0]["pinnedVersion"], drift[0]["activeVersion"]) == (1, 2)


def test_drift_never_migrates_the_instance(library, queue):
    """Re-pointing running work at a newer procedure changes the job under
    whoever is doing it, and they would have no way to know."""
    sop = a_sop(library)
    library.activate(sop.sop_id, 1)
    item = library.instantiate(sop.sop_id, queue)
    library.revise(sop.sop_id, inputs="x")
    library.activate(sop.sop_id, 2)

    library.drifted(sop.sop_id, queue)
    assert queue.get(item.id).metadata["sop_ref"]["version"] == 1


def test_finished_instances_are_not_drift(library, queue):
    """Drift is a question about work still in progress. A completed item ran
    the procedure that was current when it ran, and that is the correct record."""
    sop = a_sop(library)
    library.activate(sop.sop_id, 1)
    finish(queue, library.instantiate(sop.sop_id, queue), WorkStatus.DONE)
    library.revise(sop.sop_id, inputs="x")
    library.activate(sop.sop_id, 2)
    assert library.drifted(sop.sop_id, queue) == []


def test_no_drift_when_the_pin_is_current(library, queue):
    sop = a_sop(library)
    library.activate(sop.sop_id, 1)
    library.instantiate(sop.sop_id, queue)
    assert library.drifted(sop.sop_id, queue) == []
