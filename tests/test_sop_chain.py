"""Nested steps replace chaining (ASOP v3 §11.4).

`chain()` and `next_sop` are gone. Composition INSIDE a procedure is nesting
a step (`uses`); sequencing BETWEEN procedures is the harness's decision, not
something the plane walks on the record's behalf. Neither is resurrected
here — see the migration report for the full list of what this file used to
test and why each one is deleted.

What is worth keeping is the shape of failure the old chain tests defended:
a broken link presenting a broken process as a finished one. Translated to
`uses`, that is a nested step naming an inner ASOP `run()` cannot actually
run — because it does not exist, or because the pinned version is not
runnable (draft or retired). Either must refuse the WHOLE outer run before
anything is filed, not silently skip the nested step or hand back half a
tree.

The positive nesting case (`uses` filing the inner tree as the nested step's
children) and the decomposition-depth / self-nesting refusals already live
in `tests/test_asop_v3.py`; this file adds only the two `uses` refusals that
file has no test for.
"""

from __future__ import annotations

import pytest

from agentco.sop import SopError

DETERMINISTIC_GATE = {
    "kind": "deterministic",
    "check": "pytest -q",
    "max_park_seconds": 900,
    "on_timeout": "fail",
}


def a_one_step_asop(library, title="develop a thing"):
    return library.create(
        title,
        roles={"implementer": {"kind": "agent"}},
        steps=[{"name": "implement", "role": "implementer", "purpose": "write it",
                "gate": DETERMINISTIC_GATE}],
    )


def an_outer_asop(library, uses: dict):
    outer = library.create(
        "release",
        steps=[{"name": "develop", "uses": uses}],
    )
    return library.activate(outer.asop_id, outer.version)


def test_a_nested_step_that_uses_a_nonexistent_asop_is_refused(library, queue):
    outer = an_outer_asop(library, {"asop_id": "asop-doesnotexist", "version": 1})

    with pytest.raises(SopError) as exc:
        library.run(outer.asop_id, queue, inputs={}, bindings={})
    assert "asop-doesnotexist" in str(exc.value)
    assert queue.list() == []


def test_a_nested_step_that_uses_a_draft_is_refused(library, queue):
    """Exists, but has never been activated — the inner version `uses` names
    is not one `run()` may file work from."""
    inner = a_one_step_asop(library)  # left DRAFT
    outer = an_outer_asop(library, {"asop_id": inner.asop_id, "version": 1})

    with pytest.raises(SopError) as exc:
        library.run(outer.asop_id, queue, inputs={}, bindings={})
    assert "draft" in str(exc.value)
    assert queue.list() == []


def test_a_nested_step_that_uses_a_retired_version_is_refused(library, queue):
    """Exists, was once runnable, and was withdrawn with no successor — a
    nested step pinned to it is no more runnable than one pinned to nothing
    at all."""
    inner = a_one_step_asop(library)
    library.activate(inner.asop_id, 1)
    library.retire(inner.asop_id, author="carol", author_kind="human")
    outer = an_outer_asop(library, {"asop_id": inner.asop_id, "version": 1})

    with pytest.raises(SopError) as exc:
        library.run(outer.asop_id, queue, inputs={}, bindings={})
    assert "retired" in str(exc.value)
    assert queue.list() == []
