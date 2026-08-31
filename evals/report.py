"""Reading the ledger — paired tests, honest denominators, stdlib only.

Three rules govern every number below, and each is a way this report could
otherwise lie.

**Errored trials are excluded, not counted as failures.** A trial that never
reached its gate — a provider 500, a malformed render, a crash — is evidence
about the harness, not about the work. Folding those into the failure column
makes an unstable run read as a weak arm, and the arm that happens to run when
the provider degrades takes the blame. They are reported in their own column so
that an arm with a suspicious error count is visible rather than merely absent.

**Human-gated trials are parked, not passed and not failed.** Part III is
explicit that a parked gate is not a pass; in an unattended run it is also not a
failure of the work, so it leaves the denominator entirely.

**The paired test is exact, and it is the headline.** McNemar counts only the
tasks where two arms *disagree*, which is where all the information about a
difference lives. The concordant tasks — both passed, both failed — say the two
arms are the same on that task and contribute nothing but noise to an unpaired
comparison. The exact binomial form is used rather than the chi-squared
approximation because the discordant counts here will routinely be small, and
chi-squared is unreliable exactly there.

`successRate` follows `SopLibrary.outcomes_by_version`: `None` until something
has finished, never a measured zero.
"""

from __future__ import annotations

from math import comb
from typing import Optional

from evals.arms import Arm


def _is_scored(trial) -> bool:
    """Did this trial produce evidence about the work?"""
    if trial.error:
        return False
    if (trial.gate or {}).get("class") == "human":
        return False
    return True


def _index(trials: list, replicates: int) -> dict:
    """(arm, task) -> pass^k, over scored trials only.

    pass^k, not pass@1: an SOP that carries a definition of done is claiming
    reliability, and a task counts as passed only if every replicate passed.
    One green run out of three is a coin, not a procedure.
    """
    buckets: dict = {}
    for trial in trials:
        if not _is_scored(trial):
            continue
        buckets.setdefault((trial.arm, trial.task_id), []).append(trial.passed)
    return {
        key: (len(runs) >= replicates and all(runs))
        for key, runs in buckets.items()
        if len(runs) >= replicates
    }


def arm_rates(trials: list, replicates: int = 1) -> list:
    """Per arm: scored, passed, errored, parked, and the rate — or None."""
    rows: dict = {}
    for trial in trials:
        row = rows.setdefault(
            trial.arm,
            {"arm": trial.arm, "tasks": 0, "passed": 0, "errored": 0, "parked": 0,
             "successRate": None, "costUsd": 0.0, "unpricedTrials": 0},
        )
        if trial.error:
            row["errored"] += 1
        elif (trial.gate or {}).get("class") == "human":
            row["parked"] += 1
        if trial.cost_usd is None:
            row["unpricedTrials"] += 1
        else:
            row["costUsd"] += trial.cost_usd

    passed_index = _index(trials, replicates)
    for (arm, _task), ok in passed_index.items():
        rows.setdefault(
            arm,
            {"arm": arm, "tasks": 0, "passed": 0, "errored": 0, "parked": 0,
             "successRate": None, "costUsd": 0.0, "unpricedTrials": 0},
        )
        rows[arm]["tasks"] += 1
        rows[arm]["passed"] += 1 if ok else 0

    for row in rows.values():
        if row["tasks"]:
            row["successRate"] = round(row["passed"] / row["tasks"], 3)
        row["costUsd"] = round(row["costUsd"], 6)
        row["costIsFloor"] = row["unpricedTrials"] > 0
    return [rows[a] for a in sorted(rows)]


def mcnemar(trials: list, arm_a: str, arm_b: str, replicates: int = 1) -> dict:
    """Exact paired comparison of two arms over the tasks both completed.

    `b` is tasks where A passed and B failed; `c` the reverse. Only those move
    the result. A comparison with no discordant pairs is reported as `p = 1.0`
    with `discordant: 0`, which is the honest reading — the arms did not
    disagree anywhere, so there is nothing to test — rather than a significant
    result manufactured from a zero denominator.
    """
    index = _index(trials, replicates)
    tasks = {task for (arm, task) in index if arm in (arm_a, arm_b)}
    both = [t for t in tasks if (arm_a, t) in index and (arm_b, t) in index]

    b = sum(1 for t in both if index[(arm_a, t)] and not index[(arm_b, t)])
    c = sum(1 for t in both if not index[(arm_a, t)] and index[(arm_b, t)])
    n = b + c

    if n == 0:
        p = 1.0
    else:
        tail = sum(comb(n, i) for i in range(0, min(b, c) + 1))
        p = min(1.0, 2.0 * tail / (2 ** n))

    return {
        "armA": arm_a,
        "armB": arm_b,
        "pairedTasks": len(both),
        "aOnly": b,
        "bOnly": c,
        "discordant": n,
        "pValue": round(p, 6),
        # Named rather than left to the reader: the direction is the whole
        # point and `b > c` is not self-explanatory three months later.
        "favours": arm_a if b > c else (arm_b if c > b else None),
    }


def family_deltas(trials: list, base_arm: str, lesson_arm: str, replicates: int = 1) -> list:
    """Per family: did the lesson lift its own family, and did it cost another one?

    The regression column is the one that decides whether a lesson ships.
    `MAX_COMMON_MISTAKES` is 3, so every lesson added evicts a slot and every
    lesson occupies context on families it was never about. A lesson that lifts
    its own family by 10 points and drops two others by 5 is a net loss wearing
    a win's clothing, and only this table shows it.
    """
    index = _index(trials, replicates)
    families = {t.task_id: t.family for t in trials}
    rows: dict = {}
    for (arm, task), ok in index.items():
        if arm not in (base_arm, lesson_arm):
            continue
        row = rows.setdefault(
            families.get(task, "?"),
            {"family": families.get(task, "?"), "baseTasks": 0, "basePassed": 0,
             "lessonTasks": 0, "lessonPassed": 0},
        )
        prefix = "base" if arm == base_arm else "lesson"
        row[f"{prefix}Tasks"] += 1
        row[f"{prefix}Passed"] += 1 if ok else 0

    out = []
    for row in rows.values():
        base = row["basePassed"] / row["baseTasks"] if row["baseTasks"] else None
        lesson = row["lessonPassed"] / row["lessonTasks"] if row["lessonTasks"] else None
        row["baseRate"] = round(base, 3) if base is not None else None
        row["lessonRate"] = round(lesson, 3) if lesson is not None else None
        row["delta"] = round(lesson - base, 3) if (base is not None and lesson is not None) else None
        row["regressed"] = bool(row["delta"] is not None and row["delta"] < 0)
        out.append(row)
    return sorted(out, key=lambda r: r["family"])


def verdict(trials: list, replicates: int = 1, alpha: float = 0.05) -> dict:
    """The pre-registered bar, evaluated. Written before the result is known.

    Mirrors the project's own adoption gate in `metrics.gate1_status`: a
    falsification criterion committed in advance, evaluated mechanically, and
    allowed to come out negative. A bar that is chosen after the numbers are in
    is not a bar.
    """
    lesson = mcnemar(trials, Arm.ASOP_LESSON.value, Arm.ASOP.value, replicates)
    placebo = mcnemar(trials, Arm.PLACEBO.value, Arm.ASOP.value, replicates)
    contract = mcnemar(trials, Arm.ASOP.value, Arm.PROSE.value, replicates)
    floor = mcnemar(trials, Arm.PROSE.value, Arm.BARE.value, replicates)
    deltas = family_deltas(trials, Arm.ASOP.value, Arm.ASOP_LESSON.value, replicates)

    lesson_wins = lesson["pValue"] < alpha and lesson["favours"] == Arm.ASOP_LESSON.value
    placebo_flat = not (placebo["pValue"] < alpha and placebo["favours"] == Arm.PLACEBO.value)
    no_regression = not any(r["regressed"] for r in deltas)

    return {
        "alpha": alpha,
        "sharedLearningHolds": bool(lesson_wins and placebo_flat and no_regression),
        "clauses": {
            "lessonBeatsItsBaseVersion": lesson_wins,
            "placeboDoesNot": placebo_flat,
            "noFamilyRegressed": no_regression,
        },
        "contractBeatsProse": bool(
            contract["pValue"] < alpha and contract["favours"] == Arm.ASOP.value
        ),
        "procedureBeatsNothing": bool(
            floor["pValue"] < alpha and floor["favours"] == Arm.PROSE.value
        ),
        "comparisons": [lesson, placebo, contract, floor],
        "familyDeltas": deltas,
    }


def render_text(trials: list, replicates: int = 1) -> str:
    """A report that fits on a terminal and states its own caveats."""
    lines = ["", "ARMS", "-" * 78]
    lines.append(f"{'arm':<14}{'tasks':>7}{'passed':>8}{'rate':>8}{'err':>6}{'parked':>8}{'cost $':>12}")
    for row in arm_rates(trials, replicates):
        rate = "—" if row["successRate"] is None else f"{row['successRate']:.3f}"
        cost = f"{row['costUsd']:.4f}" + ("+" if row["costIsFloor"] else "")
        lines.append(
            f"{row['arm']:<14}{row['tasks']:>7}{row['passed']:>8}{rate:>8}"
            f"{row['errored']:>6}{row['parked']:>8}{cost:>12}"
        )

    v = verdict(trials, replicates)
    lines += ["", "PAIRED COMPARISONS  (McNemar, exact)", "-" * 78]
    lines.append(f"{'comparison':<30}{'paired':>8}{'A>B':>6}{'B>A':>6}{'p':>10}  favours")
    for c in v["comparisons"]:
        lines.append(
            f"{c['armA'] + ' vs ' + c['armB']:<30}{c['pairedTasks']:>8}"
            f"{c['aOnly']:>6}{c['bOnly']:>6}{c['pValue']:>10.4f}  {c['favours'] or '—'}"
        )

    lines += ["", "LESSON EFFECT BY FAMILY  (asop -> asop_lesson)", "-" * 78]
    lines.append(f"{'family':<28}{'base':>8}{'lesson':>9}{'delta':>9}   flag")
    for row in v["familyDeltas"]:
        base = "—" if row["baseRate"] is None else f"{row['baseRate']:.3f}"
        les = "—" if row["lessonRate"] is None else f"{row['lessonRate']:.3f}"
        delta = "—" if row["delta"] is None else f"{row['delta']:+.3f}"
        lines.append(
            f"{row['family']:<28}{base:>8}{les:>9}{delta:>9}   "
            f"{'REGRESSED' if row['regressed'] else ''}"
        )

    lines += ["", "PRE-REGISTERED VERDICT", "-" * 78]
    for clause, ok in v["clauses"].items():
        lines.append(f"  {'PASS' if ok else 'FAIL'}  {clause}")
    lines.append("")
    lines.append(f"  shared learning holds : {v['sharedLearningHolds']}")
    lines.append(f"  contract beats prose  : {v['contractBeatsProse']}")
    lines.append(f"  procedure beats bare  : {v['procedureBeatsNothing']}")
    lines += [
        "",
        "Errored trials are excluded from rates, not counted as failures; a cost",
        "marked '+' is a floor because some calls could not be priced.",
        "",
    ]
    return "\n".join(lines)
