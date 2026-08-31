"""The ASOP evaluation harness — deliberately outside the `agentco` package.

The coordination plane has zero dependencies and that is load-bearing: it is a
layer whose whole premise is that it never executes anything and never blocks
anyone. A harness that measures LLM behaviour needs an LLM SDK, retries, a
spend cap and a sandbox. Putting any of that in `agentco/` would make every
adopter's coordination layer depend on the machinery of an experiment they are
not running.

So this package is a sibling, shipped in the repo, excluded from the wheel, and
installed only via the `eval` extra. It imports `agentco`; nothing in `agentco`
imports it, and a test asserts that.
"""
