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
| 0.6.0 | v3.6 | plus `entry_gates` and `rework` (§3.8, §3.9), and the v3.6 fields that live inside existing ones: hand-off `preflight`, `subject_from.order_by` / `concurrency` / `approved_subset`, gate `on_timeout: proceed_on_default` with `default`, `cannot_evaluate_exit`, `min_cases`, and `human_approved.binds_digest` | supported, CI-tested (on PyPI since 2026-10-17) |
| 0.6.1 | v3.6.1 | the same record fields as 0.6.0 (additive release): the chain-member digest is normative (`body_digest`), the chain refusal codes incl. `human_required` are in the vocabulary, and §7.2 allows a recorded administrative ownership transfer | supported, the locked version, CI-tested (on PyPI since 2026-10-18) |

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

## A run waiting on other work: `blockedBy` (owner decision 2026-10-18)

`POST /sops/{id}/run {…, "blockedBy": ["w-…", …]}` (and `SopLibrary.run(blocked_by=…)`) files a run
whose first steps wait on other work items — how a runtime sequences one run after another.

- **What waits.** Every **root** step — a step with no in-run predecessor (`after: []`, or step 1; an
  omitted `after` means "after the step before", so it is not a root). Non-root steps wait only on their
  predecessors, as before.
- **Nested procedures (`uses`).** An inner procedure's roots wait on whatever their outer step waits on:
  the run's blockers when the `uses` step is a root, the outer predecessors otherwise. An inner procedure
  never starts ahead of the outer step it implements.
- **The parent.** Carries no extra blocker: it already waits on its steps, so it reads `pending` like any
  run whose steps have not finished. The run view adds `blockedBy` (the run's blockers) and `waitingOn`
  (`[{itemId, status}]` for each blocker not yet `done`).
- **Release.** A root becomes ready when **every** blocker is `done`. A blocker that is `failed`,
  `verify_failed` or still awaiting verification keeps it blocked — never released silently — and
  `waitingOn` names it with its status.
- **Refused before anything is filed** (`422 blocked_by_invalid`): a value that is not a list of ids, an
  empty id, an id named twice, or an id this queue does not hold. A run cannot name itself: its ids do
  not exist until it is filed.

There is no MCP tool for running a procedure (HTTP-only by design, ADR 0004), so there is no relay to
forward it. `RUN_BODY_FIELDS` in `agentco/app.py` is the route's wire map, and
`tests/test_run_blocked_by.py` pins it to `SopLibrary.run`'s and `Queue.create`'s signatures.

## Run ownership and the administrative transfer (ASOP §7.2; owner decision 2026-10-18)

**Spec wording this implements** (now normative in asop-spec 0.6.1, section 7.2): *ownership is recorded on the
run when it is filed and no **run** verb moves it; a recorded administrative transfer may.*

| Setting | Meaning |
|---|---|
| `AGENTCO_HUB_ID` | this hub's store id (default `local`); hub-filed runs are owned by it |
| `AGENTCO_STORES` | external stores and the actors that speak for them: `runtime-a=actor-1\|actor-2,…` or JSON |
| `ASOP_ADMINISTRATORS` | actors who may transfer a run (spec 0.6.1 §9); each must ALSO be a declared human. Unset = nobody (fails closed) |
| `AGENTCO_ADMINS` | **deprecated** fallback for the same, read only when `ASOP_ADMINISTRATORS` is unset. **When both are set, `ASOP_ADMINISTRATORS` wins and `AGENTCO_ADMINS` is ignored**; presence, not truthiness, decides, so declaring `ASOP_ADMINISTRATORS=` empty means nobody even if `AGENTCO_ADMINS` names someone. Remove after one minor |

- **At filing.** A run is owned by this hub, unless the filer speaks for a registered store and sends
  `ownerStore` (`POST /sops/{id}/run`); a filer who does not is refused `not_the_owner` before anything
  is filed. The record is on the run: `owner = {store, epoch, since, filed_by, history[]}`
  (`GET /runs/{id}`). A run filed before this existed reads as hub-owned, epoch 0, `backfilled`; the
  pulse writes the record down on `--apply`.
- **Who may flip a step.** For a run owned by another store, only that store's actors may claim, report or
  attest its steps — anyone else is refused **`not_the_owner`** (403, with the ownership epoch; distinct
  from `not_the_holder`, which is about a lease). `/work/pull` does not offer such steps to non-owners.
- **The pack** — `POST /runs/{id}/pack {uri, sha256}`: a location with a scheme and the sha256 of the
  pack's bytes, never its content. Set by the owner store's actors, or for a hub-owned run by its filer
  or an admin; a replaced reference is kept in `previous`.
- **The transfer** — `POST /runs/{id}/transfer {to, reason}`, declared human AND declared admin;
  `reason` required; `to` is this hub or a registered store. It needs nothing from the current owner,
  so it works when the owner's key is revoked or the owner is silent. It:
  - bumps the **ownership epoch** and appends `{at, by, from, to, reason, epoch}` to the history;
  - emits **`RunTransferred`** on the change feed;
  - **voids every live lease** on the run's steps (back to `pending`, `lease_attempt` +1, `leased_by`
    cleared, `metadata.lease_voided` says why) and **releases open steps assigned** to the former
    owner's actors, so the new owner's ordinary routing applies;
  - leaves **done** steps done, with their evidence; awaiting-verification and `verify_failed` steps
    are unchanged and answerable only under the new owner.

  After it, the former owner is fenced out: a report on a lease taken under the old epoch is
  `not_the_owner` naming both epochs, and the former store's actors may no longer claim, report or
  attest the run's steps. Transferred to the hub, the run's open steps are worked through the hub's own
  queue under normal fencing.
- **The epoch on every attestation** (§7.2: *"every flip, completion and attestation carries the epoch
  its store believes it holds"*). `POST /work/{id}/attest` takes `epoch`, the run's `owner.epoch` as the
  attester read it, and so do the client, the MCP `attest` tool and the outbox. Checked inside the
  write's lock: a mismatch is `not_the_owner` stating the current epoch, whoever sends it and even
  after a transfer back. **Optional on the wire because the spec makes it so:** absent is a store
  that predates epochs, claiming epoch 0, so it is accepted exactly while the run is at 0. Applies
  to every gate kind, hub-owned runs included; an item outside a run has no epoch. Before this the
  check ran on renew and report only, and an answer read before a hub → runtime → hub round trip
  landed after it.
- **The count (§7.2).** A run never transferred is at **epoch 0**; each transfer makes it the
  current epoch + 1, so the first transfer is epoch 1. The `filed` history entry carries 0. Every
  epoch the hub shows, stamps on a claim, states in a refusal and accepts is in this count, with no
  "0 or 1" leniency anywhere: absent is the spec's epoch 0 and nothing else.
- **The rebase.** This hub first counted from 1 at filing. `ownership.rebase_epochs` lowers every
  epoch stored in the old count by one (the run-owner record and its whole history, each claim's
  `owner_epoch` stamp, each `lease_voided` note) on JSONL, SQLite and Postgres alike. Each rewritten
  record is marked (`epoch_base: 0`, or the item key `owner_epoch_base: 0` beside a stamp), and the
  marker is the guard: a second run, or a re-run after a crash, changes nothing. The hub runs it at
  start, before the first request; the pulse runs it too (`epochsRebased`) for a node no hub has
  opened. Until it has run, reads already apply the same rule, so an unmigrated record is never
  read in the old count. `RunTransferred` events already on the feed keep the number they were
  emitted with: the feed is append-only.
- **Orphans.** The pulse reports an attention finding (`ownership.orphaned-run`) for every open run whose
  owner store is not registered, none of whose actors still holds a key (after `agentco revoke
  --apply`), or whose actors are all silent past their declared cadence (`AGENTCO_CADENCE`) — naming the
  run, the owner, and the remedy: the transfer verb.

**Status of the spec wording.** The administrative transfer landed in asop-spec 0.6.1 (§7.2, 2026-10-18) as written above, so the hub conforms in these places: the `POST /runs/{id}/transfer` route (`agentco/app.py`), `ownership.transfer` and `Queue.release_for_transfer`, the `RunTransferred` event kind, and the orphan finding's remedy text. Ownership at filing, `not_the_owner`, the epoch and the pack reference do not depend on the transfer.

**Compatibility note (refusal codes, owner decision 9; spec 0.6.1 §10).** `chain_adopt`, `approve` and
`run_transfer` refuse a non-human with `human_required` (formerly `capability_mismatch`); a transfer is
refused `transfer_refused:administrator` / `:reason` / `:same_owner` / `:malformed` (formerly
`capability_mismatch` / `transfer_invalid`); an unrecorded target manifest is `manifest_unknown` (formerly
`manifest_member_unknown`). Every other human-only verb keeps its code. For one minor the payload also
carries the old code in a deprecated `aliases` list; readers should accept the old code through v3.7.
Details: [`docs/wire.md`](wire.md#renamed-refusal-codes-owner-decision-9-2026-10-18-spec-061-10).
