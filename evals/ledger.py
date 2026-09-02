"""The trial ledger — append-only, because Layer 2 costs real money.

A behavioural eval is the expensive layer: several hundred model calls across
five arms, and the interesting effect sizes need most of them. A harness that
loses its results to a crash at trial 300, or that re-spends on resume, is not
merely inconvenient — it changes what experiment you can afford to run, and
therefore what you are willing to ask.

So every trial is written the moment it settles, one JSON object per line, and
a resumed run skips any key already present. The key is
`(run_id, task_id, arm, replicate)`: everything that makes a trial a distinct
purchase. `run_id` is in the key rather than assumed, so two runs can share a
ledger file and a later run does not silently inherit an earlier one's results
as if they were its own.

**Nothing is ever recomputed from the ledger's own rows on write.** Totals,
rates and the paired statistics all live in `report.py` and are derived on
read. A running total maintained in the file would be a second copy of a fact
the rows already carry, and the two would disagree the first time a run was
interrupted between the row and the total.

The same append-only JSONL-under-a-lock shape as `agentco.work` and
`agentco.sop`, for the same reason: a partially written line is recoverable by
a human with `tail`, and a corrupted binary index is not.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

from agentco.filelock import lock_exclusive, unlock


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Trial:
    """One task, under one arm, once. The unit of spend and the unit of score."""

    run_id: str
    task_id: str
    family: str
    arm: str
    replicate: int
    passed: bool
    gate: dict
    sop_ref: Optional[dict] = None
    executor_model: Optional[str] = None
    cost_usd: Optional[float] = None
    latency_s: Optional[float] = None
    error: Optional[str] = None
    artifact_tail: str = ""
    # For the lesson arm only: where the lessons it rendered came from —
    # `{"loop": n, "hand": n}` per `SopLibrary.lesson_provenance`. None when
    # the run had no work store to ask, which the report says out loud rather
    # than assuming either answer.
    lesson_source: Optional[dict] = None
    created_at: str = field(default_factory=_now_iso)

    @property
    def key(self) -> tuple:
        return (self.run_id, self.task_id, self.arm, self.replicate)

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)

    @classmethod
    def from_json(cls, line: str) -> "Trial":
        raw = json.loads(line)
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in raw.items() if k in known})


class Ledger:
    """Append-only trial storage with resume."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, trial: Trial) -> None:
        """One line, flushed and fsynced before the lock drops.

        The fsync is not ceremony. The failure this file exists to survive is
        the process dying, and a line sitting in the OS page cache when that
        happens is a trial that was paid for and not recorded — the exact loss
        the ledger is meant to prevent.
        """
        with open(self.path, "a", encoding="utf-8") as handle:
            lock_exclusive(handle)
            try:
                handle.write(trial.to_json() + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            finally:
                unlock(handle)

    def read_all(self) -> list:
        """Every trial, skipping unparseable lines loudly rather than silently.

        A truncated final line is the expected shape of a crash mid-append. It
        is dropped and counted, because the alternative — refusing to read the
        file at all — would make one bad line cost the other four hundred.
        """
        if not self.path.exists():
            return []
        trials, damaged = [], 0
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                trials.append(Trial.from_json(line))
            except (json.JSONDecodeError, TypeError):
                damaged += 1
        if damaged:
            print(
                f"  ledger: skipped {damaged} unreadable line(s) in {self.path} "
                f"— most likely a crash mid-append; those trials will be re-run."
            )
        return trials

    def completed_keys(self) -> set:
        """What a resumed run must not pay for twice."""
        return {t.key for t in self.read_all()}

    def __iter__(self) -> Iterator[Trial]:
        return iter(self.read_all())
