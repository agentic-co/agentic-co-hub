# Request bodies: strict and lenient routes

Owner decision 2026-10-18. Every HTTP route declares what its body may contain (`ROUTE_BODIES` in
`agentco/app.py`); `tests/test_wire.py` fails CI on a route without a declaration.

## Strict — an unknown key is refused `422 unknown_field`

Routes where a dropped key can silently remove a safety property. Before this, a misspelt `verfiy`
on `POST /work` filed an **ungated** item with a 200.

| Route | What a dropped key could remove |
|---|---|
| `POST /work` | the gate (`verify`), blockers, assignment |
| `POST /work/pull` | the capabilities a claim is checked against |
| `POST /work/{id}/report` | the fence, the idempotency key, the attestation |
| `POST /work/{id}/attest`, `/adjudicate` | the gate's evidence, the adjudication |
| `POST /scope-claims` | a claim's paths and lease |
| `POST /sops`, `/sops/{id}/revise` | a procedure's gates and chaining fields |
| `POST /sops/{id}/run` | a run's blockers, chain pin, bindings |
| `POST /chains/manifests`, `/chains/{chain}/adoptions` | a chain's pins, a person's adoption |
| `POST /approvals` | a person's approval — an absent `subset` approves **every** subject |

A key a route refuses by name with its own code (`author`, `adopted_by`, `verify` on a run, …) keeps
that refusal: the specific code wins over `unknown_field`.

## Lenient — served, with `ignored: [...]`

Every other route: release a scope claim, snapshots, digests, activate, retire, promote, propose,
and every `GET`. The response carries `ignored`, the key is logged, and `GET /metrics` counts it
under `ignoredFields` (per process: it starts at zero on restart).

## What the message says

Each unknown key gets the closest valid key and its **provenance**, from the bundled table
`agentco/field_provenance.py` (no network call, ever):

- `'entry_gates' exists since asop-spec 0.6.0 (asop/chain field); this hub has 0.5.0 installed: upgrade to asop-spec>=0.6.0`
- `'handoffs' is an asop-spec asop/chain field (since 0.5.0), … but this route does not take it here`
- `'verfiy' (did you mean 'verify'?) — 'verfiy' is unknown in every released asop-spec (0.4.0, 0.5.0, 0.6.0) and to this hub`

The table is generated from the released contracts: `python tools/asop_fields.py regenerate
[VERSION …]`. A test fails if the installed contract has a field the table does not.

## The weekly watch

`.github/workflows/asop-spec-watch.yml` (weekly, and on demand) runs `tools/asop_fields.py watch
--github`: if PyPI has an asop-spec newer than the lock, it diffs that release's record, step, gate,
attestation and chaining fields against the table and the ASOP record's columns, and opens **one**
issue — `asop-spec X.Y.Z adds fields the hub does not forward: …` — with the update checklist (lock,
allowlists, columns, provenance table, docs). An open issue with the same title is never duplicated.

## Renamed refusal codes (owner decision 9, 2026-10-18; spec 0.6.1 §10)

The hub follows the spec's 0.6.1 text (mabidoli/asop PR #9, c6aad52). Order: **who is asking is
settled before what they sent** — a non-human gets `human_required` before any refusal about the body.

| Situation | Code now | Formerly (deprecated `aliases`) |
|---|---|---|
| a non-human attempts `chain_adopt`, `approve` or `run_transfer` | **`human_required`** (403) | `capability_mismatch` |
| a declared human who is not an administrator attempts `run_transfer` | **`transfer_refused:administrator`** (403) | `capability_mismatch` |
| a transfer with no reason | **`transfer_refused:reason`** (400) | `transfer_invalid` |
| a transfer to the store that already owns the run | **`transfer_refused:same_owner`** (400) | `transfer_invalid` |
| a transfer whose `to` is not an exact name, or not this hub or a registered store | **`transfer_refused:malformed`** (400) | `transfer_invalid` |
| an impact or adoption naming a target manifest not recorded for the chain | **`manifest_unknown`** (404) | `manifest_member_unknown` |

The administrator registry is `ASOP_ADMINISTRATORS`; the deprecated `AGENTCO_ADMINS` is read only when
it is unset, and is ignored when both are set (docs/asop.md, run ownership).

`transfer_refused:stale` (the spec's rule for a `from`/`epoch` that is not the run's current owner and
epoch + 1) cannot arise here: the route computes `from` and the epoch and refuses either in the body.

**Unchanged, per §10's table of human-only verbs ("No existing code changes"):** `sop_retire` and
`promote` → `revision_policy:human_only`; a `human` gate's attestation → its existing codes; a `human`
role's step → `capability_mismatch`; and the hub's own memory verbs — pin, retire, rate, replay — keep
`capability_mismatch`, the code they shipped with. `capability_mismatch` otherwise means a capability or
role binding failure; `manifest_member_unknown` means an absent or draft member.

**Legacy uses and `aliases`.** Each renamed refusal's payload carries the old code in `aliases`,
deprecated on arrival. The spec lists the same pairs in `asop.refusals.LEGACY_USES`, which readers must
accept through v3.7 (`LEGACY_UNTIL`); the hub drops `aliases` after one minor. The codes live in the
hub's own `agentco/errors.py` (`RENAMED`), because 0.6.1 was unreleased when they shipped;
`tests/test_refusal_codes.py` fails if the installed contract spells them differently or does not list a
hub rename as a legacy use (skipped while the installed contract predates them).
