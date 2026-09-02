"""The lesson channel stops being hand-fed — and the eval can tell.

Until Phase 4 closed the loop, every `common_mistakes` entry the `asop_lesson`
arm rendered had been typed by a person, and the arm had no way to know. Now a
`bad` adjudication becomes a lesson through `propose()`, and
`SopLibrary.lesson_provenance` says, per entry, whether the loop wrote it or a
hand did — matched on the adjudication record it came from, not on the wording,
so a person typing the same sentence is still a hand. The eval ledger records
that attribution on every lesson-arm trial and the report says what the arm
actually measured, or that it does not know.

This is the P4.V gate: the lesson channel is fed by the loop rather than by a
person, and the eval can tell the difference.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentco.policy import AGENT, HUMAN
from agentco.sop import PROPOSED_KEY, SopError, lesson_text
from agentco.work import WorkStatus
from evals.arms import Arm, render
from evals.ledger import Ledger
from evals.llm import Fleet
from evals.report import lesson_channel, render_text, verdict
from evals.runner import run
from evals.tasks import TaskSet


def procedure(library, **over):
    body = {"definition_of_done": "the export matches the fixture", "validation": "diff -q out.csv expected.csv"}
    body.update(over)
    sop = library.create("export the ledger", author="dana", author_kind=HUMAN, **body)
    library.activate(sop.sop_id, 1, author="dana", author_kind=HUMAN)
    return sop


def loop_once(library, queue, sop, evidence="reported done without running the diff"):
    """One turn of the loop: execute, adjudicate bad, propose, activate the draft."""
    item = library.instantiate(sop.sop_id, queue)
    leased = queue.claim(item.id, "kofi")
    queue.report_result(item.id, leased.lease_attempt, WorkStatus.DONE)
    queue.adjudicate(item.id, "bad", evidence, adjudicator="dana")
    draft = library.propose(sop.sop_id, queue, author="agentco-lessons", author_kind=AGENT)
    library.activate(sop.sop_id, draft.version, author="dana", author_kind=HUMAN)
    return item, library.get(sop.sop_id)


# --------------------------------------------------------------------------- #
# provenance
# --------------------------------------------------------------------------- #


def test_a_lesson_the_loop_wrote_is_attributed_to_the_loop(library, queue):
    sop = procedure(library, common_mistakes=["typed by a person"])
    item, active = loop_once(library, queue, sop)
    assert active.version == 2

    provenance = library.lesson_provenance(sop.sop_id, queue)
    assert provenance["version"] == 2
    assert provenance["loopFed"] and provenance["handFed"]
    assert provenance["hand"] == ["typed by a person"]
    assert [entry["itemId"] for entry in provenance["loop"]] == [item.id]
    assert provenance["loop"][0]["lesson"] == lesson_text(item.id, "dana", "reported done without running the diff")
    assert provenance["loop"][0]["proposedIn"] == 2


def test_the_same_sentence_typed_by_a_person_is_still_a_hand(library, queue):
    """Matched on the adjudication it came from, not on the wording."""
    sop = procedure(library)
    item, _ = loop_once(library, queue, sop)
    loop_line = lesson_text(item.id, "dana", "reported done without running the diff")

    other = procedure(library, common_mistakes=[loop_line])   # a person copies the exact text
    provenance = library.lesson_provenance(other.sop_id, queue)
    assert provenance["loop"] == [] and provenance["hand"] == [loop_line]


def test_an_adjudication_nobody_proposed_yet_feeds_nothing(library, queue):
    sop = procedure(library)
    item = library.instantiate(sop.sop_id, queue)
    leased = queue.claim(item.id, "kofi")
    queue.report_result(item.id, leased.lease_attempt, WorkStatus.DONE)
    queue.adjudicate(item.id, "bad", "skipped the diff", adjudicator="dana")
    provenance = library.lesson_provenance(sop.sop_id, queue)
    assert provenance == {"sopId": sop.sop_id, "version": 1, "loop": [], "hand": [],
                          "loopFed": False, "handFed": False}


def test_provenance_is_per_version_and_the_loop_cannot_reach_back(library, queue):
    """The lesson entered at v2. Asking about v1 must not attribute it there."""
    sop = procedure(library)
    loop_once(library, queue, sop)
    assert library.lesson_provenance(sop.sop_id, queue, version=1)["loop"] == []
    assert len(library.lesson_provenance(sop.sop_id, queue, version=2)["loop"]) == 1
    with pytest.raises(SopError):
        library.lesson_provenance(sop.sop_id, queue, version=9)
    with pytest.raises(SopError):
        library.lesson_provenance("sop-deadbeef", queue)


def test_a_human_pruning_the_loops_lesson_leaves_it_out(library, queue):
    sop = procedure(library)
    loop_once(library, queue, sop)
    pruned = library.revise(sop.sop_id, common_mistakes=None, author="dana", author_kind=HUMAN)
    library.activate(sop.sop_id, pruned.version, author="dana", author_kind=HUMAN)
    provenance = library.lesson_provenance(sop.sop_id, queue)
    assert provenance["loop"] == [] and not provenance["loopFed"]


def test_a_hand_that_copies_the_loops_wording_before_the_loop_ran_is_still_a_hand(library, queue):
    """The join is on consumption, not on text: the same entry is a hand at the
    version a person typed it into, and the loop's only from the draft that
    consumed the adjudication onward."""
    sop = procedure(library)
    item = library.instantiate(sop.sop_id, queue)
    leased = queue.claim(item.id, "kofi")
    queue.report_result(item.id, leased.lease_attempt, WorkStatus.DONE)
    queue.adjudicate(item.id, "bad", "skipped the diff", adjudicator="dana")
    line = lesson_text(item.id, "dana", "skipped the diff")

    typed = library.revise(sop.sop_id, common_mistakes=[line], author="dana", author_kind=HUMAN)
    library.activate(sop.sop_id, typed.version, author="dana", author_kind=HUMAN)
    assert library.lesson_provenance(sop.sop_id, queue, version=typed.version)["hand"] == [line], (
        "nothing has consumed the adjudication yet"
    )

    assert library.propose(sop.sop_id, queue, author="agentco-lessons", author_kind=AGENT) is None, (
        "same text already there; nothing to draft"
    )
    marked = queue.get(item.id).metadata["adjudication"]
    assert marked[PROPOSED_KEY] == typed.version and marked["already_present"] is True
    assert library.lesson_provenance(sop.sop_id, queue, version=typed.version)["hand"] == [line], (
        "the pass found it there; it did not write it — still the person's"
    )
    assert library.proposals(sop.sop_id, queue)["pending"] == 0


def test_a_good_adjudication_never_counts_as_a_lesson(library, queue):
    """Good divergence feeds `proposals`, not the lesson channel. A person who
    copies the lesson wording for a good-adjudicated item is a hand."""
    sop = procedure(library)
    item = library.instantiate(sop.sop_id, queue)
    leased = queue.claim(item.id, "kofi")
    queue.report_result(item.id, leased.lease_attempt, WorkStatus.DONE)
    queue.adjudicate(item.id, "good", "the diff was redundant", adjudicator="dana")
    draft = library.propose(sop.sop_id, queue, author="agentco-lessons", author_kind=AGENT)
    assert draft.common_mistakes == [] and len(draft.proposals) == 1

    line = lesson_text(item.id, "dana", "the diff was redundant")
    typed = library.revise(sop.sop_id, common_mistakes=[line], author="dana", author_kind=HUMAN)
    provenance = library.lesson_provenance(sop.sop_id, queue, version=typed.version)
    assert provenance["loop"] == [] and provenance["hand"] == [line]


# --------------------------------------------------------------------------- #
# the arm renders the loop's lesson
# --------------------------------------------------------------------------- #


def test_the_lesson_arm_renders_what_the_loop_wrote(library, queue):
    sop = procedure(library)
    item, active = loop_once(library, queue, sop, evidence="skipped the fixture diff")
    prompt = render(Arm.ASOP_LESSON, "export the ledger", sop=active)
    assert lesson_text(item.id, "dana", "skipped the fixture diff") in prompt
    assert f"v{active.version}" in prompt


# --------------------------------------------------------------------------- #
# the eval records it, and the report says what was measured
# --------------------------------------------------------------------------- #


def _taskset(tmp_path):
    task = {
        "task_id": "t-1", "family": "f",
        "prompt": "Write a Python function `f()` returning 1. Output only the function.",
        "gate": {"class": "deterministic", "spec": "python3 -c 'print(1)'", "timeout_s": 30},
        "fixtures": {},
    }
    holdout = {**task, "task_id": "t-2", "holdout": True}
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "f.json").write_text(json.dumps([task, holdout]))
    return TaskSet.load(tmp_path)


def _fleet():
    return Fleet.load(provider="fake", models={"executor": "fake/exec", "judge": "fake/judge"})


def test_the_ledger_records_where_the_lesson_arms_lessons_came_from(library, queue, tmp_path):
    sop = procedure(library)
    _, active = loop_once(library, queue, sop)
    base = library.get(sop.sop_id, version=1)
    provenance = library.lesson_provenance(sop.sop_id, queue, version=active.version)
    source = {"loop": len(provenance["loop"]), "hand": len(provenance["hand"])}

    ledger = Ledger(tmp_path / "ledger" / "trials.jsonl")
    run(_taskset(tmp_path / "tasks"), _fleet(), ledger, "r1",
        sop_for_arm={Arm.ASOP: base, Arm.ASOP_LESSON: active},
        arms=(Arm.ASOP, Arm.ASOP_LESSON), progress=False, lesson_source=source)

    trials = ledger.read_all()
    lesson_trials = [t for t in trials if t.arm == Arm.ASOP_LESSON.value]
    assert lesson_trials and all(t.lesson_source == {"loop": 1, "hand": 0} for t in lesson_trials)
    assert all(t.lesson_source is None for t in trials if t.arm != Arm.ASOP_LESSON.value), (
        "only the lesson arm rendered a lesson channel"
    )

    channel = lesson_channel(trials)
    assert channel["loopFed"] and channel["reading"] == "the loop's lessons"
    assert verdict(trials)["lessonChannel"] == channel
    assert "loop-fed 1, hand-fed 0: the loop's lessons" in render_text(trials)


def test_a_run_with_no_provenance_reports_unknown_rather_than_either_answer(library, tmp_path):
    sop = procedure(library, common_mistakes=["typed by a person"])
    base = library.get(sop.sop_id, version=1)
    lesson = library.revise(sop.sop_id, common_mistakes=["typed by a person", "and another"],
                            author="dana", author_kind=HUMAN)
    ledger = Ledger(tmp_path / "ledger" / "trials.jsonl")
    run(_taskset(tmp_path / "tasks"), _fleet(), ledger, "r2",
        sop_for_arm={Arm.ASOP: base, Arm.ASOP_LESSON: lesson},
        arms=(Arm.ASOP, Arm.ASOP_LESSON), progress=False)
    trials = ledger.read_all()
    assert lesson_channel(trials) is None
    assert "unknown — the run recorded no provenance" in render_text(trials)


def test_a_hand_fed_run_is_named_as_measuring_a_person(library, tmp_path):
    sop = procedure(library, common_mistakes=["typed by a person"])
    base = library.get(sop.sop_id, version=1)
    ledger = Ledger(tmp_path / "ledger" / "trials.jsonl")
    run(_taskset(tmp_path / "tasks"), _fleet(), ledger, "r3",
        sop_for_arm={Arm.ASOP: base, Arm.ASOP_LESSON: base},
        arms=(Arm.ASOP_LESSON,), progress=False, lesson_source={"loop": 0, "hand": 1})
    channel = lesson_channel(ledger.read_all())
    assert not channel["loopFed"]
    assert "measures a person, not the loop" in channel["reading"]


def test_an_old_ledger_without_the_field_still_loads(tmp_path):
    from evals.ledger import Trial
    line = json.dumps({"run_id": "r", "task_id": "t", "family": "f", "arm": "asop_lesson",
                       "replicate": 0, "passed": True, "gate": {}})
    trial = Trial.from_json(line)
    assert trial.lesson_source is None
    assert lesson_channel([trial]) is None
