# ASOP — Agentic Standard Operating Procedure

**The canonical definition moved to [`ASOP.md`](https://github.com/mabidoli/asop/blob/main/ASOP.md)**
— the contract package both the Hub and the Harness import, which is where a
contract belongs.

That document is **v3**, ratified 2026-09-04. It supersedes the v2 text that lived
here. The two things v3 changed, in one line each:

- **The ASOP is the sequence; the step is what v2 called the procedure.** One ASOP
  files a tree of beads, one per step, each pinned to the version.
- **The gate is on the step, authored with the version.** A run supplies none.

Everything v2 got right — the three properties, adjudication, the revision policy,
the enforcement model, the decomposition bounds — carries forward and applies per
step. The seven design questions the v3 review settled, each with its reasoning,
are in that document's §11.

*v2 provenance, kept for the record: adversarially reviewed and cross-validated by
three frontier-model vendors 2026-08-31 (unanimous "adapt"); prior-art review the
same day (AWS Agentic SOPs/Strands, Decagon AOPs, Skan, Agent-S). See
`decisions/asop.md`.*

## Contract compatibility

The plane pins `asop-spec>=0.4.0,<1` and supports every published contract in
that range. What it can store and serve depends on which one is installed:

| `asop-spec` | Spec | What the plane stores | Status |
|---|---|---|---|
| 0.4.0 / 0.4.1 | v3.4 | the v3 record: steps, gates, roles, constraints | supported, CI-tested |
| 0.5.0 | v3.5 | plus the chaining fields `outcomes`, `outputs`, `handoffs`, `on_escalate`, `requires` (§3.7) | supported, the locked version, CI-tested |
| 0.6.0 | v3.6 | plus `entry_gates` and `rework` (§3.8, §3.9), and the v3.6 fields that live inside existing ones: hand-off `preflight`, `subject_from.order_by` / `concurrency` / `approved_subset`, gate `on_timeout: proceed_on_default` with `default`, `cannot_evaluate_exit`, `min_cases`, and `human_approved.binds_digest` | supported, CI-tested from the spec's source until it is on PyPI |

How that is kept true:

- **The schema is the table's, not the contract's.** Migration 0011 adds all
  seven chaining columns whichever contract is installed. `agentco/sqlstore.py`
  reads and writes only the columns the installed `ASOP` dataclass declares,
  and the import-time guard asserts every declared field has a column — so a
  contract that adds a field still fails the import until a migration gives it
  one.
- **A record the installed contract cannot model is quarantined, never
  truncated.** A row (SQL) or line (JSONL) carrying a field the installed
  contract does not declare is not read without it — that would run the
  procedure without its entry gate or rework bound, and the next authoring call
  would write the loss back. It is quarantined, preserved verbatim through every
  rewrite, and `revise()` refuses until the plane is upgraded. This is the same
  closed-schema rule the spec states for readers (its `MIGRATION.md`).
- **Upgrade every reader before an author writes a v3.6 field** — the spec's own
  instruction, and the one thing an operator running more than one plane process
  has to sequence.

What 0.6.0 adds beyond the record (the chain manifest, adoption, approval,
journal and chain-outcome records of §3.10 and §5.6–§5.8) is not yet stored by
the plane; `docs/roadmap.md` tracks it.
