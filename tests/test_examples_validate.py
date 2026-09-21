"""Every ASOP this repository ships is a record the plane will actually accept.

An example that no longer validates is worse than no example: it is the first
thing a new adopter copies, and the failure surfaces as *their* mistake. The
repo described ASOPs for three weeks without shipping one, so the first one it
ships gets a test that keeps it honest as the record shape moves.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentco.sop import SopLibrary, SopStatus

EXAMPLES = sorted((Path(__file__).resolve().parent.parent / "examples" / "asops").glob("*.json"))


def test_there_is_at_least_one_example():
    """If this ever passes vacuously, the suite stops proving anything here."""
    assert EXAMPLES, "no example ASOPs found to validate"


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.stem)
def test_the_example_validates_and_can_be_activated(path, tmp_path):
    body = json.loads(path.read_text())
    title = body.pop("title")
    library = SopLibrary(tmp_path / "sops.jsonl")

    asop = library.create(title, author="agentco", author_kind="agent", **body)
    assert asop.status is SopStatus.DRAFT

    activated = library.activate(asop.asop_id, asop.version,
                                 author="agentco", author_kind="agent")
    assert activated.status is SopStatus.ACTIVE


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.stem)
def test_every_step_carries_its_own_gate(path, tmp_path):
    """The property that makes it an ASOP rather than a numbered list. A step
    with no gate is advice, and advice is what the executor grades itself
    against."""
    body = json.loads(path.read_text())
    title = body.pop("title")
    library = SopLibrary(tmp_path / "sops.jsonl")
    asop = library.create(title, author="agentco", author_kind="agent", **body)

    for step in asop.steps:
        assert step.gate, f"step {step.name!r} has no gate"
        assert step.gate.get("kind") in ("deterministic", "judged", "human")
        assert step.gate.get("check"), f"step {step.name!r} has a gate with nothing to check"


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.stem)
def test_an_agent_can_activate_it_or_it_says_why_not(path, tmp_path):
    """The revision policy refuses a first activation by an agent when any step
    is human-class — deliberately, since nobody decided that step is a person's
    yet. An example that trips it would be unusable by the reader most likely to
    copy it, so either every step is agent-class, or the file is about a case
    that genuinely needs a person and this test records which."""
    body = json.loads(path.read_text())
    human_steps = [s["name"] for s in body["steps"]
                   if (s.get("gate") or {}).get("kind") == "human"]
    if human_steps:
        pytest.skip(f"{path.stem} names human gates on purpose: {human_steps}")
    title = body.pop("title")
    library = SopLibrary(tmp_path / "sops.jsonl")
    asop = library.create(title, author="an-agent", author_kind="agent", **body)
    library.activate(asop.asop_id, asop.version, author="an-agent", author_kind="agent")
