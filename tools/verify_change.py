#!/usr/bin/env python3
"""The checks a change has to pass, as a COMMAND that exits non-zero.

Written after failing the same way twice in two days, both times while saying
the opposite out loud:

  * ran `leakguard` and read the tail of its output instead of its exit code.
    It prints the same advice footer on success and on failure, so "there is a
    paragraph about suppressions" looked like "clean". CI refused the push.
  * read a *Dependabot* run's green tick as the verdict on CI, because the
    filter was by commit and not by workflow. Reported green; the CI run for
    that same commit was red.

Both are one mistake: reporting a check as passed without looking at the thing
that says whether it passed. A checklist cannot fix that, because a checklist
is read by the same party it is meant to constrain. What fixes it is a gate
somebody else's machine evaluates — and that is what an ASOP step's
`deterministic` gate is for, so this file exists to BE that command.

    python3 tools/verify_change.py --pre              # before pushing
    python3 tools/verify_change.py --ci --sha <sha>   # after pushing

Exit 0 means every named check passed. Anything else means it did not, and the
reason is on stdout. Nothing here prints "OK" for a check it skipped: a skip is
a failure with a nicer name, and the whole point is to stop those counting.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

REPO_CHECKS = "uv run --extra dev --extra server --extra mcp"
PG_CHECKS = "uv run --extra dev --extra server --extra mcp --extra postgres"


class Result:
    def __init__(self) -> None:
        self.failures: list[str] = []
        self.passed: list[str] = []

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        line = f"  [{'PASS' if ok else 'FAIL'}] {name}"
        if detail:
            line += f" — {detail}"
        print(line, flush=True)
        (self.passed if ok else self.failures).append(name)
        return ok

    def verdict(self) -> int:
        print()
        if self.failures:
            print(f"REFUSED: {len(self.failures)} check(s) failed — " + "; ".join(self.failures))
            return 1
        print(f"PASSED: {len(self.passed)} check(s)")
        return 0


def run(cmd: str, env: dict | None = None) -> tuple[int, str]:
    proc = subprocess.run(
        cmd, shell=True, capture_output=True, text=True,
        env={**os.environ, **(env or {})},
    )
    return proc.returncode, (proc.stdout + proc.stderr)


def pre_push(res: Result) -> None:
    """Everything that can be known before the change leaves this machine."""
    code, out = run("git status --porcelain")
    res.check("the working tree is committed", code == 0 and not out.strip(),
              "uncommitted changes would not be what CI tests" if out.strip() else "")

    # leakguard by EXIT CODE. Named explicitly because reading its output is
    # what went wrong: the footer about suppressions prints either way.
    code, out = run("uv run python tools/leakguard/leakguard.py --root .")
    res.check("leakguard (exit code, not output)", code == 0,
              out.strip().splitlines()[-1] if code != 0 and out.strip() else "")

    dsn = os.environ.get("AGENTCO_TEST_PG")
    if dsn:
        code, out = run(f"{PG_CHECKS} python -c "
                        "\"from tests.conftest import BACKENDS; assert 'postgres' in BACKENDS\"")
        res.check("the Postgres backend is actually active", code == 0)
        code, out = run(f"{PG_CHECKS} pytest -q")
        res.check("the suite, all three backends", code == 0, _last(out))
    else:
        code, out = run(f"{REPO_CHECKS} pytest -q")
        res.check("the suite (jsonl + sqlite only)", code == 0, _last(out))
        # NOT a pass. Stated as its own failure so a run without Postgres
        # cannot be mistaken for one with it.
        res.check("the Postgres backend was exercised", False,
                  "AGENTCO_TEST_PG is unset, so this run proves nothing about it")


def ci_for(res: Result, sha: str) -> None:
    """Every JOB of the CI workflow for this commit, not the run's headline.

    By workflow name AND by job, because both halves burned me: a repository can
    have several workflows on one commit (Dependabot's runs are green almost
    always), and a run's own conclusion can be reported before every job has
    reported its own.
    """
    code, out = run(
        "gh run list --limit 20 --json databaseId,headSha,workflowName,status,conclusion "
        f"-q '[.[] | select(.workflowName==\"CI\") | select(.headSha|startswith(\"{sha}\"))][0]'"
    )
    if code != 0 or not out.strip() or out.strip() == "null":
        res.check(f"a CI run exists for {sha}", False, "none found — was it pushed?")
        return
    run_info = json.loads(out)
    if run_info.get("status") != "completed":
        res.check(f"the CI run for {sha} has finished", False,
                  f"status is {run_info.get('status')!r}; nothing to conclude yet")
        return

    code, out = run(f"gh run view {run_info['databaseId']} --json jobs "
                    "-q '.jobs[] | \"\\(.name)=\\(.conclusion)\"'")
    if code != 0:
        res.check("the CI jobs are readable", False, _last(out))
        return
    jobs = [line for line in out.strip().splitlines() if line]
    res.check("CI reported at least one job", bool(jobs))
    for job in jobs:
        name, _, conclusion = job.partition("=")
        res.check(f"CI job {name!r}", conclusion == "success", conclusion)


def _last(out: str) -> str:
    lines = [ln for ln in out.strip().splitlines() if ln.strip()]
    return lines[-1] if lines else ""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--pre", action="store_true", help="checks that run before a push")
    ap.add_argument("--ci", action="store_true", help="confirm every CI job for a pushed commit")
    ap.add_argument("--sha", help="the commit to confirm (default: HEAD)")
    args = ap.parse_args()
    if not (args.pre or args.ci):
        ap.error("choose --pre or --ci")

    res = Result()
    if args.pre:
        print("Before the push:")
        pre_push(res)
    if args.ci:
        sha = args.sha or run("git rev-parse --short HEAD")[1].strip()
        print(f"CI for {sha}:")
        ci_for(res, sha)
    return res.verdict()


if __name__ == "__main__":
    sys.exit(main())
