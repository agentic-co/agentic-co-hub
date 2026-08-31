# The ASOP eval harness

Two claims in this repo need two different kinds of evidence, and conflating
them is the mistake this harness is shaped to avoid.

**The mechanism is software.** Versioning, pinning, immutability, drift — those
are validated deterministically, by mutation, in `tests/test_sop.py` and
`tests/test_evals_harness.py`. The bar is the one the lease protocol already
meets: *a test that cannot fail when the mechanism is removed proves nothing.*

**The claim is behavioural.** *"An ASOP makes agents do better work, and a
lesson learned by one harness transfers to another"* is an empirical claim
about LLMs. No unit test reaches it. That is what this harness is for.

## What it measures

Five arms, scored on the same fixed task set:

| Arm | What it presents | What it isolates |
|---|---|---|
| `bare` | the task, nothing else | the floor |
| `prose` | the procedure as static text, gate withheld | **the control that matters** |
| `asop` | the procedure, version pinned, gate stated | does the contract beat prose? |
| `asop_lesson` | the same, plus harvested `common_mistakes` | does the lesson transfer? |
| `placebo` | the same, plus a true-but-irrelevant lesson | is it learning, or just tokens? |

`prose` is the arm it is tempting to drop, and dropping it is how this becomes
a harness that proves something nobody disputes. Every competitor ships
procedures-as-prose. Beating `bare` proves procedures help; only beating
`prose` proves *this contract* does. A tie is a real result — it would mean
ASOP's value is accounting and governance rather than outcomes, which is worth
knowing and worth saying.

Note what has **no** arm: `versioned`. Pinning a version does not change what
the executor reads, so there is no experiment that isolates it. It is an
accounting property, validated at the mechanism layer, and it is what makes
every other number here attributable to a specific text.

## Design decisions you would otherwise have to rediscover

**Paired, not pooled.** Task difficulty varies enormously, so every arm runs
against every task and the comparison is McNemar's exact test over the tasks
where two arms disagreed. Unpaired, moving a rate from 0.60 to 0.80 needs ~85
trials per arm and 0.60 → 0.70 needs ~365. Paired, the same effect is visible
for a fraction of the spend.

**Production numbers are not eval numbers.** `SopLibrary.outcomes_by_version`
is observational: in production, v2 follows v1 on *later, different* work. Its
own docstring says an SOP applied to progressively harder cases looks like it
is degrading. The harness therefore holds the task set fixed and varies only
the arm — it never reads production counts as a result.

**The gate is the grader.** For a `deterministic` task, the procedure's own
`validation` is by definition the check that would fail if done were false. So
the production gate scores the eval, no second rubric, and the eval validates
the instrument at the same time as the arm.

**`evals/` is not `agentco/`.** The coordination plane ships with zero
dependencies and a premise that it never executes anything. This harness needs
an LLM SDK, a subprocess sandbox and a spend cap. It imports `agentco`; nothing
in `agentco` imports it, and a test asserts the arrow only points one way.

## Setup

```bash
uv sync --extra dev --extra eval
cp evals/.env.eval.example evals/.env.eval   # fill in; it is gitignored
set -a; . evals/.env.eval; set +a
python3 -m evals preflight                     # free, and run it first
```

`preflight` resolves the fleet, enforces the no-self-grading clause, reaches
the proxy, confirms both models are actually served, and loads every task —
spending nothing. The failure it exists to prevent is a run that dies at trial
40 on a mistake that was visible at trial 0.

## Run

```bash
python3 -m evals run \
  --sop-id sop-xxxxxxxx --base-version 1 --lesson-version 2 \
  --replicates 3 \
  --placebo '["Do not commit the virtualenv directory."]'

python3 -m evals report --replicates 3
```

Interrupted? Re-run with the same `--run-id`. The ledger is append-only and
fsynced per trial, so a resumed run re-spends nothing it already recorded.

`--provider fake` runs the entire loop offline with a deterministic stub — use
it to check a task set or a config change without spending.

## Writing tasks

`*.json` in `evals/tasks/`, one object or a list per file. Each task seeds its
own `check.py` fixture and gates on `python3 check.py`.

**Write the failing case first.** A gate that passes on a plausible-but-wrong
answer makes the whole run worthless, and it looks identical in the summary to
one that works. The shipped task set was mutation-checked both ways: correct
answers pass, subtly-wrong answers (a naive `split(",")`, a retry that treats a
falsy return as a failure) fail.

Every family needs at least one `holdout: true` instance. A lesson harvested
from an instance and then scored on that same instance measures recall of one
case, which is not the claim.

## Spend

Set both caps. `AGENTCO_EVAL_BUDGET_USD` is checked *before* each call, and
priced pessimistically — real prompt tokens plus the full `max_tokens` about to
be authorised, because an optimistic pre-flight check leaks on exactly the runs
that overrun. But it is the harness policing itself, and it cannot survive a
crash, a second process, or a bug in itself. Put `max_budget` on the LiteLLM
virtual key too; that one is enforced server-side.

A call LiteLLM cannot price is recorded as `None`, never `0`, and reported in
its own column. A total with unpriced calls in it is printed with a `+` and is
a floor.
