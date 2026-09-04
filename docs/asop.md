# ASOP — Agentic Standard Operating Procedure

**The canonical definition moved to [`packages/asop/ASOP.md`](../packages/asop/ASOP.md)**
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
