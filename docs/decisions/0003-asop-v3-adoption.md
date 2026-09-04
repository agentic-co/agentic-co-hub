# 0003 — The plane adopts ASOP v3

**Date:** 2026-09-04 · **Status:** adopted · **Supersedes:** nothing; extends
[`docs/decisions/asop.md`](asop.md) (the term definition)

## Context

The contract this plane implements was rewritten as **v3**
([`packages/asop/ASOP.md`](../../packages/asop/ASOP.md), ratified 2026-09-04)
after a review found that two things were true of the shipped implementation
that the v2 text did not say.

**One ASOP filed one work item.** A multi-step task was a chain of separate
records linked by `next_sop` — a reading aid, not an executable structure.
Nothing walked the chain, so the steps of a real procedure could not be gated,
attested or revised individually inside one versioned artefact. The `chain()`
walk existed to *report* how broken such a chain was, which is a good answer
to the wrong question.

**The gate was not on the procedure.** The record had no gate field. Whoever
filed the work supplied one at filing time, and in a single-operator
organisation the filer is on the executor's side. That is the executor's own
side authoring the check it will be graded by — precisely the failure the
`verified` property exists to prevent. It was not a bug in any function; it
was where the field lived.

## Decision

The plane adopts v3 in full. The grain moves: **the ASOP is the versioned
sequence, and a `Step` is what v2 called the procedure.** The gate moves onto
the step, authored with the version.

Concretely:

| v2 | v3 |
|---|---|
| `SOP` record with prose fields | `ASOP` record with `roles`, `constraints`, declared `inputs`, and an ordered list of `Step`s |
| `instantiate()` files one work item | `run(asop_id, inputs, bindings)` files a TREE — parent pinned `(asop_id, version)`, one child per step pinned `(asop_id, version, step)` |
| the filer supplies `verify` | the step carries its gate; a run passing one is refused |
| `next_sop` + `chain()` | nesting (`uses`) inside a procedure; sequencing between procedures is the harness's |
| `draft / active / superseded` | plus `retired` — human-only, keeps the record, refuses new runs |
| outcomes per version | outcomes per version **and per step** |
| lessons and proposals on the record | lessons and proposals on the **step** that earned them |
| — | `promote(run_id)` — a completed tree becomes a draft procedure |

Everything v2 got right carries forward and now applies per step: the three
properties, adjudication, the revision policy, the enforcement model, the
decomposition bounds.

## What this costs, and why it is worth it

**Two endpoints were removed rather than deprecated.**
`POST /sops/{id}/instantiate` and `GET /sops/{id}/chain`. Keeping
`instantiate` would have meant keeping the door it opens — a filer that can
author its own gate — beside the rule that closes it, and a policy with a door
beside it is not a policy. Keeping `chain` would have meant a route that walks
a link nothing writes any more, answering every call with a one-entry chain,
which is worse than a 404 because it looks like an answer.

**Every legacy row changed shape, and none was dropped.** §2.1 requires a pin
to stay resolvable forever, so migration 0009 copies each `sops` row forward
as a one-step ASOP and leaves the source table byte-identical. The upgrade has
to invent exactly one thing — a v2 record carried no gate and a v3 step must
have one — and it fails **closed** to a `human` gate. The alternatives are
worse in the same direction: a `deterministic` gate would assert that the
record's `validation` prose is a command that exits 0, which nothing ever
checked, and a `judged` gate would route to a route the operator never
declared. Only an ACTIVE v2 record can be run at all, so the blast radius is
one class of record, and one human revision replaces the placeholder with the
real gate.

**Bindings became the caller's problem, loudly.** An intake pass that used to
file work from a procedure now has to say which agent fills `implementer` and
which fills `validator`. Where a routes file is silent, every role falls back
to the assignee — right for a single-role procedure, and refused with
`constraint_unsatisfiable` for one with a separation of duties. That refusal
is the point: quietly binding one agent to both sides is exactly what the
constraint exists to prevent, and a plane that guessed would be guessing at a
roster it cannot see.

## Consequences

- `agentco/sop.py` speaks `ASOP`/`Step`; the legacy `SOP` and
  `validate_fields` remain importable **only** for `upgrade_legacy` and
  migration 0009's backfill. When those two callers are gone, so can the
  legacy record be.
- `agentco/policy.py` gains `check_asop_revision` (the three rules, per step),
  `require_human` (the fourth rule, for `retire` and `promote`), and
  `adjudicators_from_env`.
- `AGENTCO_ADJUDICATORS` joins `AGENTCO_VERIFIERS`, with the opposite default:
  undeclared verifiers fail open, undeclared adjudicators fail closed. See
  [`docs/architecture.md`](../architecture.md) for why.
- `Migration` gained an optional `backfill` callable, running inside the same
  transaction as its DDL. Migration 0009 needs it because its new rows are a
  *function* of the old ones rather than a copy, and expressing that decision
  as nested `json_object()` in two SQL dialects would put the reasoning where
  nobody reads it.
- The MCP surface is unchanged at twelve tools. v3 puts seven more verbs on
  the wire and the ceiling is full, which is a decision rather than an
  oversight — see [`0004-mcp-surface-under-asop-v3.md`](0004-mcp-surface-under-asop-v3.md).

## Alternatives considered

**Keep v2 and add steps beside it.** A record with both a flat body and a step
list has two answers to "what does this procedure say", and the shipped
implementation would have gone on filing one item from the flat one. The
review's finding was about the grain; a shape that keeps both grains does not
answer it.

**Deprecate `instantiate` rather than remove it.** See above: the deprecation
period is exactly the period in which the failure the version exists to close
is still reachable.

**Migrate legacy rows lazily and leave them in the v2 shape until touched.**
Rejected for the SQL backends: `_read_all` would then need two decoders and
every reader downstream would need to know which generation it was holding.
The JSONL store *does* upgrade on read, but only because a file store has no
migration runner — and it persists the upgraded form on the next write.
