Task files are `*.json` in this directory, one object or a list per file.

Each task seeds its own `check.py` as a fixture and gates on `python3 check.py`,
so the gate is the task's own definition of done rather than a rubric bolted on
afterwards — which is the point of the `deterministic` class. `check.py` reads
`ANSWER.md`, strips a code fence if the model added one, executes it, and
asserts. An assertion that cannot fail when the answer is wrong is worth less
than no gate at all, so write the negative case first.

`holdout: true` keeps an instance out of lesson harvesting. Every family needs
at least one, or the shared-learning result measures memorisation.
