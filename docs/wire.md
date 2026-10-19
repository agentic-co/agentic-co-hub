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
| `POST /work/{id}/attest`, `/adjudicate` | the gate's evidence, the ownership epoch, `verifyAttempt` (the item's `verify_failures`), the idempotency key, the adjudication |
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

## Per-actor routes, the derived read agent, signed queries

ADR 0008 §3.3 and §6. A credential can be confined to named routes, and a read is made *as the
signer*.

**Route allowlist** (`agentco/authz.py`). In the key table's object form, next to `owner` and
`clearance`:

```json
{"relay-1": {"secret": "…", "routes": ["attest", "events"]}}
```

`routes` names the verbs `_handle` is called with (`events`, `attest`, `work_pull`, `work_list`, …;
`authz.VERBS` is the complete list and a test keeps it equal to the verbs `app.py` uses). Checked
right after authentication and **before the body is read**; a refusal is `403 route_not_allowed`
whose remediation names the operator act (add the verb to that entry's `routes`, or remove `routes`).

- No `routes`: unrestricted, as every table written earlier. An empty list reaches nothing.
- A malformed `routes` (not a list of strings) fails closed for that actor only; signing for everyone
  else is unaffected. An unknown name grants nothing, so a typo narrows access, never widens it.
- Read on every request, so an edit applies on the next request, as revocation does.
- `transport` is a **reserved group** for the answer route and its receipts. Neither exists yet, so it
  expands to nothing today and `["transport"]` reaches nothing; name the verbs you mean until the
  envelope work fills the group in.

A transport credential therefore cannot reach `GET /work`, which returns every item's `result` and
gate metadata.

**The partition transport class** (ADR 0008 §8 B1/B2). A relay that carries a person's answers
across a partition is declared with `"class": "transport"` and a REQUIRED `owner`, the person it acts
for (its *party*, `auth.owner_of`):

```json
{"relay-a": {"secret": "…", "owner": "dana", "class": "transport"}}
```

An entry with no `owner`, or a `class` this build does not know, is not loaded: it authenticates
nobody. It cannot be the owner's own key (an entry may not own itself). A `routes` list beside it can
only narrow what follows. The class reaches only:

| Route | What it gets |
|---|---|
| `GET /hub` | `{hubId, label}`: the generated 32-hex id and the configured label (every actor may read it) |
| `GET /work?ready=1` | ready items assigned to the party or to an actor the party owns; never unassigned, never anyone else's. Any other query parameter (`agent`, `status`, …) is `400 transport_query_refused`; without `ready=1` it is `403 route_not_allowed` |
| `GET /work/{id}` | the party's vehicles (`metadata.verifies` set) and the item each verifies; anything else answers exactly like a missing id (`404 work_item_unknown`) |
| `GET /events` | `HumanKeyEnrolled` and `HumanKeyRevoked` rows only; other rows are dropped from the page (the cursor still advances) and `?kind=` naming another kind returns nothing |

The answer route and its receipts will join this set (`authz.TRANSPORT_ANSWER_VERBS`, empty today).
Every other route, including `POST /work/{id}/attest`, `POST /work`, `POST /work/pull`, `GET /runs`,
`GET /sops` and memory, is `403 route_not_allowed`. For every other actor `GET /work/{id}` returns the
item or `404 work_item_unknown`.

**The agent of a ready-work read is the signer.** `GET /work?ready=1` filters by the actor that signed
the request; the query's `agent` is never used to choose. Naming anyone else, on `?ready=1` or on a
plain list, is `400 agent_from_signature`; naming oneself is accepted. Behaviour change: a
non-operator `?ready=1` used to return the whole ready set; it now returns what that actor could pull
(unassigned items and its own), the same set `POST /work/pull` offers.
*Exception:* the registry's declared operator (`AGENTCO_REGISTRY_OPERATOR`) may name any agent or
none. It already reads the whole queue through the unfiltered list, so the filter protects nothing
from it, and it is how an operator sees queue health across lanes. A human or an administrator who is
not the operator gets no exception.

**Signed queries.** Set `ASOP_REQUIRE_SIGNED_QUERY=1` on every hub in a control-loop topology (the
rig does). A signature over the old string, without the canonical query line, is then refused `401`.
The client library (`Registry._call`, and so the MCP remote backend and the federation push) signs
the query already, and so do `agentco/conformance.py`'s loopback client and `tools/loadtest.py`;
a test runs both against a hub that sets the variable.

## Ownership epochs

`owner.epoch` on `GET /runs/{id}`, the `epoch` a transfer returns, the one a `not_the_owner`
refusal states, and the `epoch` `POST /work/{id}/attest` takes are all in the ASOP 0.6.1 §7.2
count: 0 for a run never transferred, +1 per transfer. An attest without `epoch` claims 0; an item outside a run is at epoch 0 and refuses any other value (`not_the_owner`). Records
stored in this hub's earlier count (1 at filing) are rebased once at start (`docs/asop.md`).

## Who reads what on `GET /events`

The feed is one table, filtered on the way out by who is reading:

- `MemoryChanged` opens only to a reader cleared for its label (ADR 0006); with memory off there
  are none.
- `DigestReceived` (federation, ADR 0005) is **sender-scoped**: a declared federated child
  (`ASOP_FEDERATED_CHILDREN`) reads only the digests it sent; every other reader reads all of them.
- Everything else is visible to every signed reader.

A hidden row is left out of `events` and `count`, and `nextCursor` still moves past it, so a page
the reader cannot see does not stall a poller. The MCP `events` tool reads the same function, so
the same rules apply locally and over the relay.

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
