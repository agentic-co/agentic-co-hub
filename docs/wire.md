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

## Renamed refusal codes (owner decision 9, 2026-10-18)

| Situation | Code now | Formerly | `aliases` in the payload |
|---|---|---|---|
| a non-human actor attempts a human-only verb: chain adoption, an approval, a run transfer, memory pin / retire / rate / replay | **`human_required`** (403) | `capability_mismatch` | `["capability_mismatch"]` |
| an impact or adoption names a target manifest not recorded for the chain | **`manifest_unknown`** (404) | `manifest_member_unknown` | `["manifest_member_unknown"]` |

Unchanged, with no alias: `capability_mismatch` for a capability or role binding failure (a claim or
verdict by a node lacking a declared capability; a declared human who is not an admin attempting a
run transfer); `manifest_member_unknown` for a manifest that pins an absent or draft version.

**`aliases` is deprecated on arrival** and is removed after one minor release: a client that branched on
the old code reads `aliases`, and should move to the new code now. The codes live in the hub's own
`agentco/errors.py`, because the spec release that catalogs them (asop-spec 0.6.1) was not out when
they shipped; `tests/test_refusal_codes.py` fails if the installed contract spells them differently.

**Known divergence from the spec PR, not in this change:** the spec's 0.6.1 draft maps a declared human
who is not an admin attempting a run transfer to `transfer_refused:administrator`, and the hub's
`transfer_invalid` to `transfer_refused:<rule>` (reason, same_owner, malformed, stale). The hub still
raises `capability_mismatch` and `transfer_invalid` there; aligning them is a follow-up.
