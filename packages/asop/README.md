# agentco-asop

**The ASOP contract, as code.** A procedure that is versioned, verified, and
self-revising is only useful if two independently-owned pieces of software —
a coordination plane and the harness actually running the work — agree on
what a gate, an attestation, and a procedure record *are*, without either
importing the other. This package is that agreement.

## Who imports this

- The [AgentCo coordination plane](../..) (`agentco/gates.py`,
  `agentco/errors.py`, `agentco/sop.py` are thin shims over this package,
  keeping every existing caller and test unchanged).
- The [AgentCo Harness](https://github.com/agentic-co/agentic-co-harness), the
  standalone execution runtime, from its P2 (adopting this schema in place
  of its own `VERIFY_KEYS`/`validate_verify`).
- Anyone else building a plane or a harness that wants to speak the same
  gate shape as either, without depending on the code that runs either one.

## What's here

| Module | Contract |
|---|---|
| `asop.errors` | `Refusal` — the one exception type: a stable machine `code`, a human `message`, and a `remediation` sentence. Every refusal in this package (and, by re-export, in the plane) is one of these. |
| `asop.gates` | The unified gate schema. `validate_gate(payload, *, require=(), max_park_seconds_ceiling=None)` normalises a `deterministic` / `judged` / `human` gate — merging the plane's park-clock fields with the Harness's staged-check and runtime-hint fields into one shape — or refuses. `validate_attestation`, `attestation_passes`, `retry_decision` cover the evidence side. |
| `asop.sop` | The SOP **record** contract: `SopStatus`, the `SOP` dataclass, `validate_fields`. Not the store — drafting, revising, activating, and instantiating work from a template is plane- or harness-side policy, layered on top of this record shape. |
| `asop.refusals` | The refusal-code vocabulary — every machine-readable `code` a `Refusal` carries across the reference plane, named once with a one-line meaning, for a reader scanning codes rather than call sites. |

## The gate schema

One canonical field decides the shape: `kind` (`deterministic \| judged \|
human`). `class` is accepted as a read alias — the Harness's historical
name for the same field — and normalised output always uses `kind`.
Sending both, disagreeing, is refused.

Three field groups:

- **core** (always required) — `kind`, and exactly one of `check` (one
  command) or `checks` (an ordered, staged list; a one-element `checks`
  normalises into `check`).
- **clock** (the plane's park clock) — `max_park_seconds`, `on_timeout`,
  `escalate_to` (only with `on_timeout: escalate`), `verifier` (required
  for `human`, refused for `deterministic`, optional for `judged`).
  Declared as a whole or not at all — a gate carrying some of these fields
  but not both `max_park_seconds` and `on_timeout` is always refused,
  regardless of whether a caller requires the clock at all.
- **execution** (the Harness's runtime hints) — `cwd`, `timeout_s`,
  `rubric` (`judged` only), `judge_route` (`judged` only).

Whether the clock group is mandatory is the one thing that differs by
caller, and the one knob `validate_gate` exposes: `require=("clock",)` is
the plane's convention today; `require=()` is a caller with no park-clock
concept, such as the Harness before it adopts one. The 30-day park-clock
ceiling is **not** a package constant — it is the plane's own decision
about its own registry, passed in as `max_park_seconds_ceiling`. A caller
with no ceiling passes `None`.

Output is always fully normalised: every known field present (`None` where
absent), plus `"schema_version": 1`.

## Versioning

`asop.SCHEMA_VERSION` covers the gate schema's normalised shape. It bumps
only on a breaking change — a new required field, a renamed one, a changed
type. An additive optional field or a new refusal code does not bump it,
because existing readers already tolerate both. See `asop/__init__.py`.

## Install

Not published standalone yet — used as a workspace member of the parent
repo (`[tool.uv.sources]` in the root `pyproject.toml` points `agentco-asop`
at `packages/asop`, editable). A harness in a different repo installs it
the ordinary way once published: `pip install agentco-asop` /
`uv add agentco-asop`.

Standard library only — see `../../CONTRIBUTING.md`'s dependency rule. This
package is imported by both sides of the contract, so a dependency here is
forced on both.
