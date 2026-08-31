"""The arms — how one procedure is presented to the executor, five ways.

**Not every ASOP property has a behavioural arm, and pretending otherwise would
be the harness's own first lie.** Of the three:

* **Versioned** is an *accounting* property. Pinning a version does not change
  what the executor reads, so there is no arm that isolates it and no
  experiment that could. What it buys is that every other number here is
  attributable to a specific text — which is why it is validated at Layer 1, by
  mutation, and not here.
* **Verified** has a behavioural claim: telling an executor the check it will
  face changes what it produces. That is `PROSE` versus `ASOP`.
* **Self-revising** has the strongest claim: a lesson harvested from one
  harness's failures lifts another's success rate. That is `ASOP` versus
  `ASOP_LESSON`, with `PLACEBO` as the control that keeps it honest.

The arm that will be tempting to drop is `PROSE`, and dropping it is how this
harness would end up proving something nobody disputes. Every competitor ships
procedures-as-prose — AWS Agentic SOPs, Decagon AOPs. Beating `BARE` proves
procedures help. Only beating `PROSE` proves *this* contract does. If they tie,
that is a real result and the honest response is to narrow the README's claim
to accounting and governance, which is still worth having.

`PLACEBO` carries a lesson that is true, well-formed, and irrelevant to the
task family. If it lifts scores as much as the real lesson, the harness is
measuring the presence of extra tokens rather than the transfer of knowledge,
and every shared-learning number in the report is void. It is the cheapest test
here that could embarrass the claim, so the runner schedules it early rather
than as an afterthought.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from agentco.sop import TEXT_FIELDS, SOP


class Arm(str, Enum):
    BARE = "bare"
    PROSE = "prose"
    ASOP = "asop"
    ASOP_LESSON = "asop_lesson"
    PLACEBO = "placebo"


# Order matters only for reporting; the runner randomises execution order.
ALL_ARMS = tuple(Arm)

# Arms that need a procedure. BARE is the floor and deliberately gets nothing.
ARMS_WITH_SOP = (Arm.PROSE, Arm.ASOP, Arm.ASOP_LESSON, Arm.PLACEBO)

_LABEL = {
    "purpose": "Purpose",
    "trigger": "Trigger",
    "entry_check": "Before you start, confirm",
    "inputs": "Inputs",
    "definition_of_done": "Definition of done",
    "validation": "How this will be verified",
    "write_back": "Where the result goes",
}


class ArmContractError(ValueError):
    """The arm cannot be rendered as configured."""


def _fields_block(sop: SOP, include_validation: bool) -> str:
    lines = []
    for name in TEXT_FIELDS:
        if name == "validation" and not include_validation:
            continue
        value = getattr(sop, name, None)
        if value:
            lines.append(f"{_LABEL[name]}: {value}")
    return "\n".join(lines)


def _mistakes_block(mistakes: list) -> str:
    if not mistakes:
        return ""
    body = "\n".join(f"  - {m}" for m in mistakes)
    return f"\nKnown failure modes on this procedure, learned from earlier runs:\n{body}\n"


def render(
    arm: Arm,
    task_prompt: str,
    sop: Optional[SOP] = None,
    placebo_mistakes: Optional[list] = None,
) -> str:
    """Build the executor prompt for one arm.

    The procedure text is byte-identical across `PROSE`, `ASOP`, `ASOP_LESSON`
    and `PLACEBO` except for the one thing each arm is meant to add. If the
    arms differed in wording as well, the comparison would measure prompt
    engineering and attribute the result to the contract.
    """
    if arm in ARMS_WITH_SOP and sop is None:
        raise ArmContractError(
            f"arm {arm.value!r} needs a procedure and got none. Only "
            f"{Arm.BARE.value!r} runs without one — it is the floor."
        )

    if arm is Arm.BARE:
        return task_prompt

    if arm is Arm.PROSE:
        # The competitor's shape: the procedure as advice, with no stated gate
        # and no version. Deliberately omits `validation` — a prose SOP that
        # announced its own check would already be half an ASOP, and the arm
        # would stop being a control.
        return (
            f"Follow this procedure.\n\n{_fields_block(sop, include_validation=False)}\n\n"
            f"---\n\nTask: {task_prompt}"
        )

    header = (
        f"You are working under procedure {sop.sop_id} v{sop.version}. It is "
        f"versioned; the version above is pinned to this task and will not change "
        f"under you. You will be checked against the verification below, by a "
        f"party that is not you."
    )

    mistakes: list = []
    if arm is Arm.ASOP_LESSON:
        mistakes = list(sop.common_mistakes or [])
        if not mistakes:
            raise ArmContractError(
                f"arm {Arm.ASOP_LESSON.value!r} renders the lesson channel, but "
                f"{sop.sop_id} v{sop.version} carries no common_mistakes. "
                f"Running it anyway would make it a duplicate of "
                f"{Arm.ASOP.value!r} and report a null result as a tie."
            )
    elif arm is Arm.PLACEBO:
        mistakes = list(placebo_mistakes or [])
        if not mistakes:
            raise ArmContractError(
                "the placebo arm needs placebo_mistakes — a true, well-formed "
                "lesson that is irrelevant to this task family. Without it "
                "there is no control on the shared-learning result."
            )

    return (
        f"{header}\n\n{_fields_block(sop, include_validation=True)}\n"
        f"{_mistakes_block(mistakes)}\n---\n\nTask: {task_prompt}"
    )
