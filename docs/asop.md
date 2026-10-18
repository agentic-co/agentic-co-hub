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
| 0.5.0 | v3.5 | plus the chaining fields `outcomes`, `outputs`, `handoffs`, `on_escalate`, `requires` (§3.7) | supported, CI-tested |
| 0.6.0 | v3.6 | plus `entry_gates` and `rework` (§3.8, §3.9), and the v3.6 fields that live inside existing ones: hand-off `preflight`, `subject_from.order_by` / `concurrency` / `approved_subset`, gate `on_timeout: proceed_on_default` with `default`, `cannot_evaluate_exit`, `min_cases`, and `human_approved.binds_digest` | supported, the locked version, CI-tested (on PyPI since 2026-10-17) |

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

## Chain manifests, adoptions and approvals (v3.6, `agentco/chains.py`)

Three v3.6 records name a person or name the store, so the plane holds them:
it is the store whose digests a manifest pins, and it holds the operator's
declared humans. All three need asop-spec 0.6.0; on an older contract every
route below refuses `contract_unsupported` (501) rather than storing a record
it cannot validate.

| Route | Who | What |
|---|---|---|
| `GET /sops/{id}?version=` | anyone | now also returns `digest`: this store's `sha256:` over the version's validated body — what a manifest member must carry (§3.10, "a member's digest is the store's") |
| `POST /chains/manifests` `{manifest}` | any authenticated engine | `asop.chain-manifest/1`, checked by the contract; every member must be a non-draft version this store holds, at this store's digest. Idempotent on the manifest digest. The FIRST manifest for a chain is the one it starts under; later ones take effect only by adoption |
| `GET /chains/{chain}` | anyone | the manifest in force, every manifest recorded, the adoptions in order; `chain: null` for a chain nobody pinned |
| `GET /chains/{chain}/impact?to=` | anyone | the computed impact (`adoption_impact`) of moving to manifest `to` — what a person reads before adopting, and what an agent cites when proposing |
| `POST /chains/{chain}/adoptions` `{to, reason?}` | declared human only | `asop.adoption/1`. `adopted_by` is the signature, `from` is the manifest in force, the impact is computed; a body naming any of them is refused. Compare-and-set on the manifest in force, so two concurrent adoptions cannot fork a chain |
| `POST /approvals` `{artefact, digest, subset?}` | declared human only | `asop.approval/1`, append-only. `approved_by` / `approved_at` are the signature and the clock |
| `GET /approvals?artefact=&digest=` | anyone | the history, the latest, and with `digest`, whether the latest binds it (`current`) — the check `human_approved.binds_digest` makes |
| `POST /sops/{id}/run` `{chain, ...}` | as before | a run IN a pinned chain files at the version the chain's manifest pins — not the active one — and a version or procedure the manifest does not pin is refused `adoption_required`. The run records `chain: {chain, manifest}` under a reserved key |

Feed kinds: `ChainManifestRecorded`, `ChainAdopted`, `ArtefactApproved`.

**Not stored, on purpose: the chain outcome record (§5.8) and the step journal
(§5.6).** The outcome is "computed from the ledger, never typed", and the plane
holds no ledger of a runtime's runs; accepting a typed outcome would store
exactly what the spec forbids. Both arrive with journal ingest (§7.2: "a
journal is what a non-owning plane receives"), tracked in `docs/roadmap.md`.

## Could not evaluate — the third gate outcome (spec v3.6 §3.3, §5.4)

A `deterministic` gate may declare `cannot_evaluate_exit` (3 by convention) and `min_cases`. An
attestation that exits with that status, or exits 0 having run fewer than `min_cases` cases
(`cases_run`), **could not evaluate** the work: the check could not judge it, which is neither a pass
nor the executor's failure. Classified by the contract's own `asop.gates.gate_outcome`, on the
`report` path and on the executor's re-attest path alike. What the plane does today (option A, owner
decision 2026-10-18):

| | could not evaluate | real failure (any other nonzero exit) |
|---|---|---|
| status | `verify_failed` | `verify_failed` |
| releases dependents | **no** | no |
| `verify_failures` | **unchanged** | +1 |
| `metadata.verify_retry.decision` | `"re-run"` (`outcome: "cannot_evaluate"`) | `"fix"`, then `"escalate"`, then `"stop"` |
| `metadata.verify_cannot_evaluate` | `{pending: true, count, last: {check, exit_status, cases_run, …}, reason}` | `pending` set false if present; `count` kept as history |
| pullable again | **yes** — offered by `ready()` / `POST /work/pull`, claimed under a **new fence** (`lease_attempt` +1; a report on the old attempt is refused) | no — held until its own gate passes |
| `GET /sops/{id}/outcomes` | its own column, `cannotEvaluate`, counted in `unresolved`; never in `verifyFailed` or `failed` | `verifyFailed` |
| pulse | fact `cannotEvaluate`: items pending a re-run | unchanged |
| quarantine (re-run offer unclaimed past the gate's park time) | reason `re-run unclaimed after could-not-evaluate` | `re-verify unanswered` |

A gate that declares neither field keeps exactly its two outcomes: exit 3 there is a failure and is
charged, as in v3.5. `verify_cannot_evaluate` is a reserved metadata key: no caller can set it.

The spec's own bead status for this case is `cannot_evaluate` (§5.2). The plane represents it as
`verify_failed` plus the flag; the dedicated status is designed and deferred — `docs/roadmap.md`,
"A dedicated `cannot_evaluate` status".
