"""Gate execution — the reference implementation of ASOP Part III.

`docs/asop.md` Part III says a conforming implementation must pin five things
per deterministic gate: **identity**, **environment**, **isolation**,
**timeout**, and **idempotency**. Nothing in `agentco/` implements any of them,
because the coordination plane deliberately never executes a check. The eval
harness has no such luxury — scoring an arm *is* running the gate — so the
contract gets its first executable form here, and the harness is the thing that
proves the clauses are implementable rather than aspirational.

Each clause, and the failure it exists to prevent:

* **Timeout is mandatory and has no default of "none".** A gate that can hang
  is not a control, it is a deadlock with good intentions. Expiry is *failure*,
  never a retry and never a skip — a check that timed out did not pass, and
  recording it as "unknown" would let a hang launder itself into a non-result.

* **The environment is constructed, not inherited.** A check receives no
  ambient credentials. This is not paranoia about the check itself, which the
  ASOP's author wrote at plan time; it is about what a *model-authored artifact
  under test* can reach when the check invokes it. The harness holds a live LLM
  API key in its own environment, and handing that to a subprocess spawned per
  trial, several hundred times, is how an eval run becomes an incident.

* **The gate cannot write the record it is verifying.** Enforced by giving the
  check a working directory that is not the ledger's, and by the runner
  recording the result rather than the check reporting it. A gate that can edit
  its own outcome is the self-grading failure in a different costume.

* **Idempotency is asserted, not assumed.** `rerun_check` runs a passing gate a
  second time and refuses the result if the two disagree. A gate whose verdict
  depends on how many times it has run cannot support the retry semantics the
  contract promises, and finding that out during scoring is far cheaper than
  finding it out from a number nobody can reproduce.

Judged gates route through `Fleet`, whose constructor already refuses an
executor and judge that resolve to the same model. The no-self-grading clause
is therefore enforced one layer down, at configuration time, rather than being
re-checked at every call site and eventually forgotten at one of them.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

from evals.llm import JUDGE, Fleet

# Names whose VALUE never reaches a check, matched case-insensitively as a
# substring. Deny-by-default would be safer still, but a check that cannot find
# `python3` fails for a reason that has nothing to do with the work under test,
# and a harness that reports that as a gate failure is producing noise.
SECRET_NAME_PATTERN = re.compile(
    r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|PAT|COOKIE|SESSION|AUTH)",
    re.IGNORECASE,
)

# Carried through, because a check with no PATH cannot run an interpreter.
ENV_ALLOW = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "TZ", "SHELL", "USER")


class GateClass(str, Enum):
    DETERMINISTIC = "deterministic"
    JUDGED = "judged"
    HUMAN = "human"


class GateContractError(ValueError):
    """The gate is malformed. Refused at the write boundary, never no-op'd."""


@dataclass
class GateResult:
    """What a gate decided, and enough evidence to argue with it.

    `passed` is a plain bool with no third state on purpose. Part III makes
    expiry a failure, so there is no "inconclusive" for a timeout to hide in.
    """

    gate_class: str
    passed: bool
    detail: str
    exit_code: Optional[int] = None
    timed_out: bool = False
    duration_s: Optional[float] = None
    stdout_tail: str = ""
    stderr_tail: str = ""
    judge_model: Optional[str] = None
    judge_cost_usd: Optional[float] = None

    def as_dict(self) -> dict:
        return {
            "class": self.gate_class,
            "passed": self.passed,
            "detail": self.detail,
            "exitCode": self.exit_code,
            "timedOut": self.timed_out,
            "durationS": self.duration_s,
            "stdoutTail": self.stdout_tail,
            "stderrTail": self.stderr_tail,
            "judgeModel": self.judge_model,
            "judgeCostUsd": self.judge_cost_usd,
        }


@dataclass
class Gate:
    """A gate as authored — at plan time, by someone who is not the executor."""

    gate_class: GateClass
    # deterministic: the command. judged: the rubric. human: who must ack.
    spec: str
    timeout_s: int = 120
    cwd: Optional[str] = None
    # Extra env names permitted beyond ENV_ALLOW. Values still pass the secret
    # filter, so listing `OPENAI_API_KEY` here does not smuggle it through.
    env_extra: tuple = field(default_factory=tuple)

    def __post_init__(self):
        if isinstance(self.gate_class, str):
            self.gate_class = GateClass(self.gate_class)
        if not isinstance(self.spec, str) or not self.spec.strip():
            raise GateContractError(
                f"a {self.gate_class.value} gate with a blank spec claims to "
                f"check something and checks nothing. Give it a command, a "
                f"rubric, or a named person — or author no gate at all, which "
                f"is an honest state and this is not."
            )
        self.spec = self.spec.strip()
        if not isinstance(self.timeout_s, int) or self.timeout_s <= 0:
            raise GateContractError(
                f"timeout_s must be a positive integer, got {self.timeout_s!r}. "
                f"ASOP Part III makes the timeout mandatory: a gate with no "
                f"deadline is a deadlock, not a control."
            )


def scrubbed_env(extra: tuple = ()) -> dict:
    """The environment a check gets: allowlisted names, minus anything secret-shaped."""
    allowed = set(ENV_ALLOW) | set(extra)
    return {
        name: value
        for name, value in os.environ.items()
        if name in allowed and not SECRET_NAME_PATTERN.search(name)
    }


def _tail(text: str, limit: int = 2000) -> str:
    """Keep the end, which is where a failing check says why.

    Truncating the head loses the traceback's origin, and truncating the tail
    loses the assertion that fired. The assertion is the one being read.
    """
    text = text or ""
    if len(text) <= limit:
        return text
    return "…(truncated)…\n" + text[-limit:]


def run_deterministic(gate: Gate, workdir: Path) -> GateResult:
    """Run the command. Exit 0 is the only pass; expiry is a failure."""
    import time

    env = scrubbed_env(gate.env_extra)
    cwd = Path(gate.cwd) if gate.cwd else workdir
    started = time.monotonic()
    try:
        proc = subprocess.run(
            gate.spec,
            shell=True,
            cwd=str(cwd),
            env=env,
            capture_output=True,
            text=True,
            timeout=gate.timeout_s,
        )
    except subprocess.TimeoutExpired as exc:
        return GateResult(
            gate_class=GateClass.DETERMINISTIC.value,
            passed=False,
            detail=(
                f"gate exceeded its {gate.timeout_s}s deadline. Part III: "
                f"expiry is failure, never a hang and never a retry."
            ),
            timed_out=True,
            duration_s=round(time.monotonic() - started, 3),
            stdout_tail=_tail(exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")),
            stderr_tail=_tail(exc.stderr.decode() if isinstance(exc.stderr, bytes) else (exc.stderr or "")),
        )
    except OSError as exc:
        # The check could not be launched at all. Distinct from a check that
        # ran and failed, and a reader who cannot tell them apart will go
        # looking for a bug in the work rather than in the gate.
        return GateResult(
            gate_class=GateClass.DETERMINISTIC.value,
            passed=False,
            detail=f"gate could not be launched: {exc}. This is a harness fault, not a work failure.",
            duration_s=round(time.monotonic() - started, 3),
        )

    duration = round(time.monotonic() - started, 3)
    return GateResult(
        gate_class=GateClass.DETERMINISTIC.value,
        passed=proc.returncode == 0,
        detail=f"exit {proc.returncode}",
        exit_code=proc.returncode,
        duration_s=duration,
        stdout_tail=_tail(proc.stdout),
        stderr_tail=_tail(proc.stderr),
    )


def rerun_check(gate: Gate, workdir: Path, first: GateResult) -> Optional[str]:
    """Assert idempotency by running a settled gate again. Returns a complaint, or None.

    Only worth the second run when the first one settled: re-running a gate
    that could not be launched tells you nothing new and costs the same.
    """
    if first.timed_out or first.exit_code is None:
        return None
    second = run_deterministic(gate, workdir)
    if second.passed != first.passed:
        return (
            f"gate is not idempotent: consecutive runs returned "
            f"{first.passed} then {second.passed} (exit {first.exit_code} then "
            f"{second.exit_code}). A verdict that depends on how many times it "
            f"has run cannot support retry, and any number computed from it is "
            f"unreproducible."
        )
    return None


JUDGE_PROMPT = """You are grading one piece of work against a fixed rubric.

The rubric was written before the work existed and may not be renegotiated.
Grade only against it. Do not reward effort, length, or good intentions.

RUBRIC
{rubric}

WORK UNDER REVIEW
{artifact}

Reply with JSON only, no prose around it:
{{"passed": true|false, "reason": "<one sentence, citing the rubric clause that decided it>"}}
"""


def run_judged(gate: Gate, artifact: str, fleet: Fleet) -> GateResult:
    """Grade against a rubric fixed at authoring time, on a route that is not the executor's.

    The no-self-grading clause is enforced by `Fleet.load()`, which refuses a
    configuration whose executor and judge resolve to the same model. It is not
    re-checked here: a rule enforced in two places is a rule that will be
    enforced in one of them after the next refactor.

    A judge that returns unparseable output **fails the gate** rather than
    erroring the trial. That is the conservative direction — a grader that
    cannot state a verdict has not granted one — and it keeps a flaky judge
    visible in the results instead of silently thinning the sample.
    """
    prompt = JUDGE_PROMPT.format(rubric=gate.spec, artifact=artifact)
    completion = fleet.complete(JUDGE, [{"role": "user", "content": prompt}])
    verdict, reason = _parse_verdict(completion.text)
    return GateResult(
        gate_class=GateClass.JUDGED.value,
        passed=verdict,
        detail=reason,
        judge_model=completion.model,
        judge_cost_usd=completion.cost_usd,
        duration_s=completion.latency_s,
        stdout_tail=_tail(completion.text, 800),
    )


def _parse_verdict(text: str) -> tuple:
    """Pull the verdict out of a judge's reply, tolerating a code fence but not a guess."""
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = candidate.strip("`")
        candidate = candidate.split("\n", 1)[-1] if "\n" in candidate else candidate
        candidate = candidate.rsplit("```", 1)[0]
    start, end = candidate.find("{"), candidate.rfind("}")
    if start != -1 and end > start:
        candidate = candidate[start : end + 1]
    try:
        payload = json.loads(candidate)
        passed = payload["passed"]
    except (json.JSONDecodeError, KeyError, TypeError):
        return False, (
            "judge returned no parseable verdict; scored as a failure. A grader "
            "that cannot state a verdict has not granted one."
        )
    if not isinstance(passed, bool):
        return False, f"judge returned a non-boolean verdict {passed!r}; scored as a failure."
    return passed, str(payload.get("reason", "")).strip() or "(no reason given)"


def run(gate: Gate, workdir: Path, artifact: str, fleet: Optional[Fleet] = None) -> GateResult:
    """Dispatch on class. Human gates park; they do not pass."""
    if gate.gate_class == GateClass.DETERMINISTIC:
        result = run_deterministic(gate, workdir)
        complaint = rerun_check(gate, workdir, result)
        if complaint:
            result.passed = False
            result.detail = complaint
        return result
    if gate.gate_class == GateClass.JUDGED:
        if fleet is None:
            raise GateContractError("a judged gate needs a Fleet to route the judge call")
        return run_judged(gate, artifact, fleet)
    return GateResult(
        gate_class=GateClass.HUMAN.value,
        passed=False,
        detail=(
            f"human gate parked, awaiting {gate.spec}. A parked gate is not a "
            f"pass — in an unattended eval run it is an excluded trial, and the "
            f"report counts it separately rather than as a failure of the work."
        ),
    )
