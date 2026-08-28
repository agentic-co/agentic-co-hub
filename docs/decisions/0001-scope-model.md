# 0001 — Scope is a prefix set at declared minimum depth

**Status:** accepted · **Date:** 2026-08-28

> A decision with no revisit condition is doctrine. Every record here carries one.

## Context

A scope claim answers one question: *does my work intersect yours?* Everything
the primitive is worth depends on that answer being **precise**, because the
failure mode is not a wrong answer — it is a correct answer nobody reads.

The first version of this design shipped scope claims as the headline feature
with the scope field **undefined**, while stating elsewhere that path globs are
"a filter, not a partition, because path globs do not partition cleanly."

Those two statements cannot both stand. If the natural representation does not
compose, and composition is the entire value, then the representation has to be
chosen deliberately rather than left to whatever callers happen to send.

## Alternatives

**(a) Ship it undefined.** Four fields, a `POST` and a `GET`, and let the scope
format settle by usage. Fastest to build.

**(b) Path globs.** Familiar, expressive, and what people will reach for.

**(c) `(repo, path-prefix set)` at directory granularity, with a minimum depth
and prefix-overlap intersection.**

## Decision

**(c).** Specifically:

- Scope is `(repo, path-prefix set)`. **Prefixes, not globs.**
- A prefix must name at least `k` path segments below the repo root, `k = 2`
  initially. A claim on `src/` or on the repo root is **refused**, with a
  remediation naming the requirement.
- Intersection is **segment-wise prefix overlap**, computed as a set operation.
- A conflict fires only between **two different holders**, and carries **both
  intents**.
- Enforcement is **advisory**. A conflict is information delivered to two
  people; nothing is blocked.

## Reasoning

**The minimum depth is the whole decision.** Without it, ten people each holding
a claim on `src/` is not the pathological case — it is the *default* case, the
one you get when everybody does the obvious thing. Every claim then intersects
every other, conflicts fire on every write, and within days everyone learns the
registry is noise. A notification channel that has been classified as noise
cannot be reclassified later; that reputation is permanent.

**Globs were rejected because they do not compose.** `src/**/test/*.py` versus
`src/billing/**` — deciding whether those intersect is not a set operation, it
is a decision procedure, and one that gets edge cases wrong quietly. Prefixes
intersect exactly when one is a segment-wise prefix of the other. That is four
lines and no edge cases.

**Segment-wise, not string-wise.** `src/budget` and `src/budgeting` share a
string prefix and share no directory. A `startswith` check calls that a
conflict, and a false conflict costs more than a missed one: a missed conflict
is a merge you were going to have anyway, while a false one teaches people the
tool is wrong about them.

**Both intents, because the pair is the information.** *Prototype vs implement*
is usually fine. *Implement vs implement* usually is not. Reporting only the
incumbent's intent throws away half of what the reader needs.

**Advisory, because blocking is where the political cost lives.** Making
concurrency visible is most of the value and none of the cost — but a registry
that refused someone's first claim would be uninstalled the same week, and it
would deserve to be.

## Revisit condition

The registry publishes its own precision: **conflicts fired ÷ conflicts a holder
acted on**, over a trailing window.

If precision sits below the floor, the granularity rule is wrong and **the fix
is `k`, not more claims and not a quieter notification.** Raising `k` makes
claims more specific and intersections rarer, which is the lever that actually
moves the number.

The report **refuses to recommend** below a minimum sample of fired conflicts.
Telling an operator to change `k` on the evidence of two conflicts would spend
credibility the report needs the first time it says something expensive.

## Consequences

- Callers must know which directories they are working in before claiming. That
  is a small real cost, and it is the cost that buys the precision.
- A claim spanning a whole repository is not expressible. That is intentional: it
  is the claim that makes every other claim meaningless.
- `k = 2` is unvalidated on day one. There is no data yet, and the revisit
  condition is the mechanism for getting some.
