"""The trial loop — paired by construction, randomised within a task.

**Pairing is the loop shape, not an analysis choice made afterwards.** Task
difficulty here varies enormously: one instance is a rename, the next is a
concurrency bug. Comparing arm means across a pool of tasks throws that
variance into the error term, and the sample size needed to see through it is
brutal — unpaired, moving a gate pass rate from 0.60 to 0.80 needs roughly 85
trials per arm, and 0.60 to 0.70 needs about 365. Paired, only the tasks where
the arms *disagree* carry information, and the same effect is visible in a
fraction of the spend. That is the difference between a weekend and a quarter,
so the runner iterates tasks on the outside and runs every arm against each one
before moving on.

**Arm order within a task is shuffled per task, seeded from the run and the
task id.** Fixed order lets any position effect — a warmed cache, a rate limit
that bites late, a provider degrading through the run — load entirely onto
whichever arm always runs last. Seeding from `(run_id, task_id)` rather than
from the clock keeps the shuffle reproducible: the same run resumed tomorrow
makes the same choices.

**Each trial gets a fresh working directory.** Contamination across arms is the
quiet killer of a within-task design: arm 2 seeing the file arm 1 wrote does
not look like a bug, it looks like arm 2 doing well. The directory is seeded
from the task's fixtures, the model's answer is written into it, and the gate
runs there — so a `deterministic` gate is checking the artifact and nothing
else.
"""

from __future__ import annotations

import random
import shutil
import tempfile
import traceback
import uuid
from pathlib import Path
from typing import Optional

from agentco.sop import SOP
from evals import gate as gate_mod
from evals.arms import ALL_ARMS, Arm, render
from evals.ledger import Ledger, Trial
from evals.llm import EXECUTOR, BudgetExceeded, Fleet
from evals.tasks import Task, TaskSet

# Where the model is told to put its answer. Named rather than inferred: a gate
# that has to guess which file the artifact landed in fails for reasons that
# have nothing to do with the work.
ARTIFACT_NAME = "ANSWER.md"

SYSTEM = (
    "You are completing one work item. Produce the artifact the task asks for "
    "and nothing else — no preamble, no explanation of what you are about to "
    "do. If the task asks for code or a file, output only its contents."
)


class RunAborted(RuntimeError):
    """The run stopped early. The ledger holds everything settled before it."""


def _seeded_arms(run_id: str, task_id: str, arms: tuple) -> list:
    order = list(arms)
    random.Random(f"{run_id}:{task_id}").shuffle(order)
    return order


def _seed_workdir(task: Task, root: Path) -> Path:
    workdir = root / f"{task.task_id}-{uuid.uuid4().hex[:8]}"
    workdir.mkdir(parents=True)
    for rel, contents in task.fixtures.items():
        target = workdir / rel
        # A fixture path escaping the workdir would let a task write anywhere
        # the harness can reach. Refused rather than normalised: a task
        # definition that does this is wrong, not ambiguous.
        if not str(target.resolve()).startswith(str(workdir.resolve())):
            raise RunAborted(
                f"task {task.task_id}: fixture path {rel!r} escapes the trial "
                f"working directory. Refused."
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(contents, encoding="utf-8")
    return workdir


def run_trial(
    task: Task,
    arm: Arm,
    replicate: int,
    fleet: Fleet,
    run_id: str,
    root: Path,
    sop: Optional[SOP] = None,
    placebo_mistakes: Optional[list] = None,
) -> Trial:
    """One purchase: render, execute, gate, record. Never raises for a work failure."""
    workdir = _seed_workdir(task, root)
    sop_ref = sop.ref if sop is not None else None
    try:
        prompt = render(arm, task.prompt, sop=sop, placebo_mistakes=placebo_mistakes)
        completion = fleet.complete(
            EXECUTOR,
            [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
        )
        (workdir / ARTIFACT_NAME).write_text(completion.text, encoding="utf-8")
        result = gate_mod.run(task.gate, workdir, completion.text, fleet=fleet)
        return Trial(
            run_id=run_id,
            task_id=task.task_id,
            family=task.family,
            arm=arm.value,
            replicate=replicate,
            passed=result.passed,
            gate=result.as_dict(),
            sop_ref=sop_ref,
            executor_model=completion.model,
            cost_usd=completion.cost_usd,
            latency_s=completion.latency_s,
            artifact_tail=completion.text[-800:],
        )
    except BudgetExceeded:
        # The one exception that must stop the run rather than be recorded as a
        # failed trial. A budget stop is not evidence about the work, and
        # writing it as `passed: false` would poison every rate computed after.
        raise
    except Exception as exc:
        # Everything else is recorded as a harness fault and excluded from the
        # rates by `report.py`. A trial that errored is not a trial the work
        # failed, and folding the two together is how an unstable harness reads
        # as a weak arm.
        return Trial(
            run_id=run_id,
            task_id=task.task_id,
            family=task.family,
            arm=arm.value,
            replicate=replicate,
            passed=False,
            gate={"class": task.gate.gate_class.value, "passed": False, "detail": "not reached"},
            sop_ref=sop_ref,
            error=f"{type(exc).__name__}: {exc}",
            artifact_tail=traceback.format_exc()[-800:],
        )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def run(
    taskset: TaskSet,
    fleet: Fleet,
    ledger: Ledger,
    run_id: str,
    sop_for_arm: dict,
    replicates: int = 1,
    arms: tuple = ALL_ARMS,
    placebo_mistakes: Optional[list] = None,
    progress: bool = True,
) -> dict:
    """Score every task under every arm, resuming whatever the ledger already holds.

    `sop_for_arm` maps an arm to the SOP version it presents — `ASOP` to the
    base version, `ASOP_LESSON` to the revision carrying the harvested lesson.
    Passing the same version for both is not refused here because `arms.render`
    already refuses it, and a check duplicated in two places is one that will
    survive in the wrong one.
    """
    done = ledger.completed_keys()
    planned = len(taskset) * len(arms) * replicates
    executed = skipped = 0
    stopped: Optional[str] = None

    with tempfile.TemporaryDirectory(prefix="agentco-eval-") as tmp:
        root = Path(tmp)
        for task in taskset:
            for replicate in range(replicates):
                for arm in _seeded_arms(run_id, task.task_id, arms):
                    key = (run_id, task.task_id, arm.value, replicate)
                    if key in done:
                        skipped += 1
                        continue
                    try:
                        trial = run_trial(
                            task, arm, replicate, fleet, run_id, root,
                            sop=sop_for_arm.get(arm),
                            placebo_mistakes=placebo_mistakes,
                        )
                    except BudgetExceeded as exc:
                        stopped = str(exc)
                        break
                    ledger.append(trial)
                    executed += 1
                    if progress:
                        mark = "PASS" if trial.passed else ("ERR " if trial.error else "fail")
                        print(f"  [{executed:>4}/{planned}] {mark}  {task.task_id:<28} {arm.value}")
                if stopped:
                    break
            if stopped:
                break

    return {
        "runId": run_id,
        "planned": planned,
        "executed": executed,
        "skippedAlreadyDone": skipped,
        "spend": fleet.spend.as_dict(),
        "stoppedEarly": stopped,
    }
