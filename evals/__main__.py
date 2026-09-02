"""`python3 -m evals <command>` — preflight, run, report.

`preflight` exists because the failure this harness must not have is a run that
dies at trial 40 on a configuration mistake that was visible at trial 0. It
resolves the fleet, enforces the no-self-grading clause, reaches the proxy,
confirms both models are actually served, and loads every task file — all
without spending anything. Run it first; it is free and it is the difference
between a bad afternoon and a bad minute.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from pathlib import Path

from agentco.sop import SopLibrary, resolve_sop_store
from evals.arms import Arm
from evals.ledger import Ledger
from evals.llm import ENV_API_BASE, ENV_API_KEY, ENV_MODEL, EXECUTOR, JUDGE, Fleet, LlmError
from evals.report import render_text, verdict
from evals.runner import run as run_trials
from evals.tasks import TaskSet

DEFAULT_TASKS = Path(__file__).parent / "tasks"
DEFAULT_LEDGER = Path("evals-out/trials.jsonl")


def _fleet(args) -> Fleet:
    return Fleet.load(
        budget_usd=args.budget,
        provider=getattr(args, "provider", None),
    )


def cmd_preflight(args) -> int:
    problems: list = []

    try:
        fleet = _fleet(args)
        print(f"  executor : {fleet.models[EXECUTOR]}")
        print(f"  judge    : {fleet.models[JUDGE]}   (must differ — enforced)")
        print(f"  api_base : {fleet.api_base or '(direct to vendor)'}")
        print(f"  api_key  : {'set' if fleet.api_key else 'NOT SET'}")
        print(f"  budget   : {('$%.2f' % fleet.budget_usd) if fleet.budget_usd else 'UNCAPPED'}")
    except LlmError as exc:
        print(f"  fleet    : REFUSED — {exc}")
        return 2

    if fleet.api_base and not fleet.api_key:
        problems.append(
            f"an api_base is set but {ENV_API_KEY} is not. A LiteLLM proxy will "
            f"answer 401 on every call, several hundred times."
        )
    if fleet.budget_usd is None:
        problems.append(
            "no budget cap. Set AGENTCO_EVAL_BUDGET_USD, and set max_budget on "
            "the virtual key too — the client-side cap cannot survive a crash."
        )

    served = _served_models(fleet)
    if served is not None:
        print(f"  proxy    : reachable, {len(served)} model(s) served")
        for role in (EXECUTOR, JUDGE):
            bare = fleet.models[role].split("/", 1)[-1]
            if bare not in served:
                problems.append(
                    f"{role} model {bare!r} is not served by the proxy. "
                    f"Available: {', '.join(sorted(served)[:12])}"
                    + (" …" if len(served) > 12 else "")
                )

    try:
        taskset = TaskSet.load(args.tasks)
        print(f"  tasks    : {len(taskset)} across {len(taskset.families)} "
              f"famil{'y' if len(taskset.families) == 1 else 'ies'} "
              f"({len(taskset.holdout())} holdout)")
    except Exception as exc:
        problems.append(f"tasks: {exc}")

    if problems:
        print("\n  PROBLEMS")
        for p in problems:
            print(f"    - {p}")
        return 1
    print("\n  preflight clean — nothing was spent.")
    return 0


def _served_models(fleet: Fleet):
    """Ask the proxy what it serves. Returns None when there is no proxy to ask."""
    if not fleet.api_base or not fleet.api_key:
        return None
    try:
        import urllib.request

        req = urllib.request.Request(
            fleet.api_base.rstrip("/") + "/v1/models",
            headers={"Authorization": f"Bearer {fleet.api_key}"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload = json.loads(resp.read())
        return {m["id"] for m in payload.get("data", [])}
    except Exception as exc:
        print(f"  proxy    : could not list models ({exc})")
        return None


def cmd_run(args) -> int:
    fleet = _fleet(args)
    taskset = TaskSet.load(args.tasks)
    ledger = Ledger(args.ledger)
    library = SopLibrary(resolve_sop_store(args.sop_store))

    base = library.get(args.sop_id, args.base_version)
    lesson = library.get(args.sop_id, args.lesson_version)
    if base is None:
        print(f"no SOP {args.sop_id!r} version {args.base_version}", file=sys.stderr)
        return 2
    if lesson is None:
        print(f"no SOP {args.sop_id!r} version {args.lesson_version}", file=sys.stderr)
        return 2

    sop_for_arm = {
        Arm.PROSE: base,
        Arm.ASOP: base,
        Arm.ASOP_LESSON: lesson,
        Arm.PLACEBO: base,
    }
    placebo = json.loads(args.placebo) if args.placebo else None

    lesson_source = None
    if args.work_store:
        from agentco.work import Queue

        provenance = library.lesson_provenance(args.sop_id, Queue(args.work_store), args.lesson_version)
        lesson_source = {"loop": len(provenance["loop"]), "hand": len(provenance["hand"])}
        print(f"  lesson channel of v{args.lesson_version}: "
              f"{lesson_source['loop']} loop-fed, {lesson_source['hand']} hand-fed")
    else:
        print("  lesson channel provenance: unknown (pass --work-store to attribute the lessons)")

    run_id = args.run_id or f"run-{uuid.uuid4().hex[:8]}"
    print(f"  run {run_id}: {len(taskset)} tasks x {len(Arm)} arms x {args.replicates} replicate(s)")
    summary = run_trials(
        taskset, fleet, ledger, run_id,
        sop_for_arm=sop_for_arm,
        replicates=args.replicates,
        placebo_mistakes=placebo,
        lesson_source=lesson_source,
    )
    print(json.dumps(summary, indent=2))
    if summary["stoppedEarly"]:
        print("\n  Resume with the same --run-id; the ledger re-spends nothing.")
        return 1
    print(render_text(ledger.read_all(), args.replicates))
    return 0


def cmd_report(args) -> int:
    trials = Ledger(args.ledger).read_all()
    if not trials:
        print(f"no trials in {args.ledger}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(verdict(trials, args.replicates), indent=2))
    else:
        print(render_text(trials, args.replicates))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python3 -m evals", description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)

    def common(sp):
        sp.add_argument("--tasks", default=str(DEFAULT_TASKS))
        sp.add_argument("--ledger", default=str(DEFAULT_LEDGER))
        sp.add_argument("--budget", type=float, default=None, help="USD cap for this run")
        sp.add_argument("--provider", default=None, help="'fake' for a dry loop with no spend")
        sp.add_argument("--replicates", type=int, default=1, help="k for pass^k")

    p_pre = sub.add_parser("preflight", help="check config and reach the proxy; spends nothing")
    common(p_pre)
    p_pre.set_defaults(func=cmd_preflight)

    p_run = sub.add_parser("run", help="score every task under every arm")
    common(p_run)
    p_run.add_argument("--sop-id", required=True)
    p_run.add_argument("--base-version", type=int, required=True)
    p_run.add_argument("--lesson-version", type=int, required=True)
    p_run.add_argument("--sop-store", default=None)
    p_run.add_argument("--run-id", default=None, help="reuse to resume a stopped run")
    p_run.add_argument("--placebo", default=None, help='JSON list, e.g. \'["..."]\'')
    p_run.add_argument(
        "--work-store", default=None,
        help="the work store holding the adjudications; lets the report say whether the lesson arm's lessons were loop-fed or hand-fed",
    )
    p_run.set_defaults(func=cmd_run)

    p_rep = sub.add_parser("report", help="read the ledger and render the verdict")
    common(p_rep)
    p_rep.add_argument("--json", action="store_true")
    p_rep.set_defaults(func=cmd_report)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except LlmError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
