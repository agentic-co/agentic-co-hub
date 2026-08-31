"""Task families and instances — the fixed set every arm is scored against.

The single most dangerous thing about evaluating a procedure is that in
production, versions are **sequential, not randomised**. v2 is used after v1,
on later work, which is usually different work. `SopLibrary.outcomes_by_version`
says so in its own docstring: an SOP applied to progressively harder cases looks
like it is degrading. Those numbers are observational and confounded, and no
amount of care in reading them fixes that — so the harness does not read them.
It holds the task set fixed and varies only the arm.

**Family and instance are separate on purpose.** A family is a kind of work a
procedure claims to govern; an instance is one case of it. Scoring a lesson
against the family it was learned from measures memorisation, so a lesson
harvested from instances of family A must be scored on *held-out* instances of
A — and on family B, which it should not affect at all. `holdout` and the
family split are what make the regression check in `report.py` possible; without
them, "did the lesson hurt anything else" has no denominator.

A task carries its own gate. That is not a harness convenience — it is the
ASOP contract doing double duty: for a `deterministic` task the procedure's own
`validation` field is the check that would fail if done were false, so the
production gate *is* the eval's grader. No second rubric, no separate scoring
model, and the eval validates the instrument at the same time as the arm.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Optional

from evals.gate import Gate, GateClass


class TaskContractError(ValueError):
    """The task definition is malformed. Refused rather than defaulted."""


@dataclass
class Task:
    """One scoreable case: a prompt, a gate, and the family it belongs to."""

    task_id: str
    family: str
    prompt: str
    gate: Gate
    # Held out of lesson harvesting. A lesson learned from an instance and then
    # scored on that same instance measures recall of a specific case, which is
    # not the claim being tested.
    holdout: bool = False
    # Files the trial's working directory is seeded with before the model runs.
    # Relative path -> contents.
    fixtures: dict = field(default_factory=dict)
    notes: Optional[str] = None

    @classmethod
    def from_dict(cls, raw: dict, source: Path) -> "Task":
        missing = [k for k in ("task_id", "family", "prompt", "gate") if not raw.get(k)]
        if missing:
            raise TaskContractError(
                f"{source}: task is missing {', '.join(missing)}. Every task "
                f"needs an id, a family to be grouped by, a prompt, and a gate "
                f"— a task with no gate cannot be scored, and one with no "
                f"family cannot be checked for regression."
            )
        gate_raw = dict(raw["gate"])
        gate = Gate(
            gate_class=GateClass(gate_raw.pop("class", "deterministic")),
            spec=gate_raw.pop("spec", ""),
            timeout_s=int(gate_raw.pop("timeout_s", 120)),
            cwd=gate_raw.pop("cwd", None),
            env_extra=tuple(gate_raw.pop("env_extra", ()) or ()),
        )
        if gate_raw:
            raise TaskContractError(
                f"{source}: unknown gate field(s) {', '.join(sorted(gate_raw))}"
            )
        return cls(
            task_id=raw["task_id"],
            family=raw["family"],
            prompt=raw["prompt"],
            gate=gate,
            holdout=bool(raw.get("holdout", False)),
            fixtures=dict(raw.get("fixtures") or {}),
            notes=raw.get("notes"),
        )


@dataclass
class TaskSet:
    """Every task in one run, indexed by family."""

    tasks: list

    def __iter__(self) -> Iterator[Task]:
        return iter(self.tasks)

    def __len__(self) -> int:
        return len(self.tasks)

    @property
    def families(self) -> list:
        return sorted({t.family for t in self.tasks})

    def by_family(self, family: str) -> list:
        return [t for t in self.tasks if t.family == family]

    def holdout(self, family: Optional[str] = None) -> list:
        return [t for t in self.tasks if t.holdout and (family is None or t.family == family)]

    def training(self, family: Optional[str] = None) -> list:
        """The instances a lesson may be harvested from — never the holdout."""
        return [t for t in self.tasks if not t.holdout and (family is None or t.family == family)]

    @classmethod
    def load(cls, path: Path | str) -> "TaskSet":
        """Read every `*.json` under a directory, or one file.

        Duplicate ids are refused rather than last-one-wins. A silently
        overwritten task means a run whose denominator does not match its task
        directory, and that discrepancy surfaces as an unreproducible number
        weeks later rather than as an error now.
        """
        path = Path(path)
        files = sorted(path.glob("*.json")) if path.is_dir() else [path]
        if not files:
            raise TaskContractError(
                f"no task files under {path}. A run with an empty task set "
                f"reports 100% of nothing and looks like a pass."
            )
        tasks: list = []
        seen: dict = {}
        for f in files:
            raw = json.loads(f.read_text())
            for entry in raw if isinstance(raw, list) else [raw]:
                task = Task.from_dict(entry, f)
                if task.task_id in seen:
                    raise TaskContractError(
                        f"duplicate task_id {task.task_id!r} in {f} and "
                        f"{seen[task.task_id]}. Ids are how a resumed run knows "
                        f"what it already paid for; two tasks sharing one would "
                        f"make the ledger skip real work."
                    )
                seen[task.task_id] = f
                tasks.append(task)
        return cls(tasks)
