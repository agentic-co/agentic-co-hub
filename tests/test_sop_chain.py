"""SOPs as a chained process, not four isolated paragraphs.

A procedure that only says what *done* means leaves the two most expensive
questions unanswered: did I have what I needed before I started, and who is
waiting for what I produced. `entry_check` and `write_back` are those halves,
and they are what makes chaining work — one step's `write_back` is the next
step's `entry_check`, so a chain is a real handoff rather than a list.

`validation` is separate from `definition_of_done` on purpose. The definition
is the claim; validation is the check that would FAIL if the claim were false.
Collapsing them is how "done" quietly comes to mean "I believe I finished".

The tests worth having here are the ones about a chain that LIES. Both ways it
can lie look identical to a short chain:

  * a link naming an SOP that does not exist, and
  * a link to one with no active version, which cannot be instantiated.

A walk that simply stopped at either would present a broken process as a
finished one.
"""

from __future__ import annotations

import pytest

from agentco.sop import SopContractError, SopLibrary


def active(library, title, **body):
    sop = library.create(title, **body)
    library.activate(sop.sop_id, sop.version)
    return sop


# --------------------------------------------------------------------------- #
# The three fields that make a procedure runnable rather than merely described
# --------------------------------------------------------------------------- #


def test_a_procedure_carries_entry_validation_and_write_back(library):
    sop = active(
        library,
        "Develop a thing",
        purpose="Build it",
        entry_check="You have the work item and a reproducible local stack. If either is "
                    "missing, ask before starting rather than assuming.",
        definition_of_done="The suites pass and the change is proven locally.",
        validation="Re-run the affected suites and paste the real output; a suite that "
                   "could not run is named with its reason.",
        write_back="The outcome goes on the work item, and the evidence into "
                   "ai-tasks/<item>/evidence/.",
    )
    read = library.get(sop.sop_id)
    assert read.entry_check.startswith("You have the work item")
    assert read.validation.startswith("Re-run")
    assert read.write_back.startswith("The outcome")


def test_a_blank_new_field_is_refused_like_every_other(library):
    """A present-but-blank field claims to answer a question it does not."""
    with pytest.raises(SopContractError) as caught:
        library.create("x", purpose="p", entry_check="   ")
    assert "entry_check" in str(caught.value)


def test_an_unknown_field_is_still_refused_and_lists_the_allowed_ones(library):
    with pytest.raises(SopContractError) as caught:
        library.create("x", purpose="p", steps="do the thing")
    message = str(caught.value)
    assert "steps" in message
    assert "entry_check" in message and "write_back" in message


# --------------------------------------------------------------------------- #
# Chaining
# --------------------------------------------------------------------------- #


def test_a_chain_walks_from_one_procedure_to_the_next(library):
    test_sop = active(library, "Test it", purpose="verify", entry_check="a built thing")
    dev = active(library, "Build it", purpose="build",
                 write_back="hand the built thing to test", next_sop=test_sop.sop_id)

    steps = library.chain(dev.sop_id)
    assert [s["title"] for s in steps] == ["Build it", "Test it"]
    assert all(s["state"] == "active" for s in steps)
    # The handoff is legible from the chain itself, without opening each step.
    assert steps[0]["write_back"] == "hand the built thing to test"
    assert steps[1]["entry_check"] == "a built thing"


def test_a_link_to_a_nonexistent_sop_is_reported_not_silently_ended(library):
    dev = active(library, "Build it", purpose="build", next_sop="sop-deadbeef")
    steps = library.chain(dev.sop_id)
    assert steps[-1] == {"sop_id": "sop-deadbeef", "state": "missing"}, (
        "a broken link that ends the walk quietly presents a broken process as "
        "a finished one"
    )


def test_a_link_to_a_draft_is_reported_as_inactive_with_its_status(library):
    """Distinct from missing: it exists, and cannot be instantiated from."""
    draft = library.create("Test it", purpose="verify")
    dev = active(library, "Build it", purpose="build", next_sop=draft.sop_id)

    steps = library.chain(dev.sop_id)
    assert steps[-1]["state"] == "inactive"
    assert steps[-1]["latestStatus"] == "draft"
    assert steps[-1]["title"] == "Test it"


def test_a_cycle_terminates_the_walk_and_says_so(library):
    """test -> fix -> test is a real intent expressed badly, so report it."""
    a = active(library, "Test it", purpose="verify")
    b = active(library, "Fix it", purpose="fix", next_sop=a.sop_id)
    closed = library.revise(a.sop_id, next_sop=b.sop_id)  # now a -> b -> a
    library.activate(closed.sop_id, closed.version)

    steps = library.chain(a.sop_id)
    assert steps[-1]["state"] == "cycle"
    assert [s.get("title") for s in steps[:2]] == ["Test it", "Fix it"]


def test_a_revision_carries_the_link_forward(library):
    """Editing one line must not silently unchain the process."""
    tail = active(library, "Test it", purpose="verify")
    dev = active(library, "Build it", purpose="build", next_sop=tail.sop_id)

    revised = library.revise(dev.sop_id, purpose="build it better")
    library.activate(revised.sop_id, revised.version)
    assert revised.next_sop == tail.sop_id
    assert [s["title"] for s in library.chain(dev.sop_id)] == ["Build it", "Test it"]


def test_an_unchained_procedure_is_a_chain_of_one(library):
    solo = active(library, "Do it", purpose="just do it")
    steps = library.chain(solo.sop_id)
    assert len(steps) == 1
    assert steps[0]["state"] == "active"


def test_chaining_an_id_that_does_not_exist_at_all(library):
    assert library.chain("sop-nope") == [{"sop_id": "sop-nope", "state": "missing"}]
