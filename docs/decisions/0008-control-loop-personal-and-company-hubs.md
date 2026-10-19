# 0008 — The owner's control loop: pull first, one partition per company, answers as signed envelopes

**Date:** 2026-10-18 · **Status:** proposed · **Supersedes:** nothing; narrows
[`0007-hub-network-graph-and-channels.md`](0007-hub-network-graph-and-channels.md)
for one topology and amends its direction table for the case where the hub that
owns the work also executes it (§2)

> A decision with no revisit condition is doctrine. Every record here carries one.

Nothing in this record is built. 0005's push-only digest is still the only
hub-to-hub path that exists.

## Context

One person works for several organisations at once — a consultant with three or
four client companies. 0007's boundary rule gives each company its own hub,
because each is a different data owner. Each company hub files that company's
work from its own tracker and meetings, and a runner on that company's machine
executes it. The person also has a personal hub and a personal runner on their
own machine.

What the person needs is one loop: questions and parked gates from every company
reach them in one place, and their answers and instructions go back to the right
company, without switching machines — and without any company's content meeting
another's.

Four facts about the code and the contract shape the answer:

1. **The question record already exists.** For every parked or failed human gate,
   the pulse files a *vehicle*: a work item titled `Verify: <title>`, assigned to
   the gate's named verifier, keyed `verify:<item>:<attempt>`, retired when the
   attempt moves (`agentco/verifiers.py`). Its own docstring names the missing
   piece: the vehicle exists and nothing tells the person.
2. **A human gate is answered only by the named person** (ASOP §5.3, "naming is
   not authenticating"), and a plane may route a gate it does not own but may not
   answer it (§7.3).
3. **Attestations are not signed.** Request authentication is per-actor HMAC with
   shared secrets. `docs/known-issues.md` already says why that cannot support
   attestation across organisations: both parties hold the secret, so neither can
   show a third party who wrote a verdict, and the stored record carries no
   signature.
4. **Any key on a hub is full access today**, the attestation path does not
   check the ownership epoch that §7.2 requires on every attestation, and a hub's
   id defaults to the same string on every hub.

## Alternatives

**(a) Push first, answer with the person's own HMAC key.** Company hubs push
notices to the personal hub; the person attests with their per-company actor key.
Push puts a store of company titles on the personal hub, and needs new code before
anything works. The HMAC answer leaves the company hub able to write the person's
verdict itself, which is fact 3 unsolved.

**(b) A relay answers on the person's behalf.** Rejected: a relay's signature is not
the person's (§5.3), a plane answering a gate it does not own is what §7.3 forbids,
and a `human` role means a person looks (§3.6). Nothing in the spec permits a
delegate to answer a human gate.

**(c) Pull first; answers as signed envelopes.** Each company partition on the
person's runner polls its own company hub for the vehicles assigned to the person.
A deterministic switchboard renders a fixed projection. Answers are signed by a
per-company key the company hub knows only by its public half, and carried by a
transport credential limited to that route.

## Decision

**(c).**

### 1. One partition per company; a switchboard with no model

The personal runner keeps **one partition per company**: that company's
credentials, signing key, sessions and any model context live there and nowhere
else (0006 §2, 0007 §3). A partition polls its company hub for vehicles assigned
to the person and hands the switchboard exactly `{label, ref, title, ageSeconds}`:
`label` from the partition's configuration, never from hub data; `ref` the vehicle
key; `title` the vehicle's subject title.

The **switchboard is deterministic and holds no model credential and no company
credential.** On selection it passes `ref` to the partition named by `label`.
Drill-in is a live read by that partition from its own hub. The personal hub holds
no company content and is not on the loop.

### 2. "Parent" is per channel

| Channel | From | To | Authoritative |
|---|---|---|---|
| notices | company hub | the person's view (the personal hub once push exists) | company hub |
| answers | the person, through that company's partition | company hub | company hub (§7.3) |
| work | the person, through that company's partition | company hub | company hub, once filed |
| memory | — | — | never between company hubs |

The personal hub is the **parent on the notices channel only**. It never owns a
company's work, never answers a company's gate, and never enrolls a key. This
amends 0007's direction table, which sends answers back toward the hub that sent
the work, for the case where the owning hub also executes it.

### 3. Answers are signed envelopes

**What is signed.** A payload of `type` (`asop.answer/1`), `hub` (the owning
company hub's id, §3.1), `item`, `attempt`, `stage`, `epoch`, `verdict` (`{passed,
reason}`, §5.3's shape), an optional `choice`, `signer`, `key_id`, a 128-bit
`nonce` and an integer `issued_at`. The bytes signed are the type line followed by
the payload serialized with the spec's **normative canonical JSON** (§3.10: keys by
code point, no whitespace, no floats) — the same function the spec package exports
for digests, not a second serialization. Every field is present (an absent field is
refused with a schema code, never defaulted); integers are integers and never
booleans; duplicate keys are refused at parse. Test vectors for the payload are
published so a signer in another language produces the same bytes.

#### 3.1 The hub id is generated, not configured

Today a hub's id is an operator string whose default is `local` on every hub, so it
binds nothing. The envelope's `hub` is a **128-bit id generated at first start and
persisted**, exposed by the hub, pinned by the person's partition when a key is
enrolled and stored beside the enrolled key. The configured name stays a label.
**Envelopes cannot be enabled while a hub's id is `local`.**

#### 3.2 Keys, and what they do and do not give

Ed25519 by default; ECDSA P-256 (low-S) allowed so that keys in a platform secure
enclave or a hardware token can sign; the algorithm is bound to the enrolled key.
The company hub holds only the **public** key per declared human. A per-human HMAC
secret was rejected: the hub would hold the same secret and could write the
person's verdict, which is fact 3.

Stated exactly: **the company hub cannot produce a signature that verifies under the
key the person published. It owns the key directory, so it can enrol a key it
controls — a substitution that is detectable, not prevented.** The person keeps
every envelope they signed and the fingerprint of every key they published; every
enrolment and revocation is an event on the hub's feed (`HumanKeyEnrolled`,
`HumanKeyRevoked`), and the partition compares the keys the hub reports for the
person with its own. True non-repudiation needs two things this record does not
build: the envelope carried in the attestation record by the spec (Open 1), and a
public-key directory the hub does not control.

**One key per (person, company hub)**, generated by that hub's partition and never
reused for another hub, enrolled by that hub's operator from a public key the
person hands over out of band, fingerprint compared on a second channel. Never over
the parent; there is no enrolment verb on the wire. A key for a non-human actor, or
one public key under two humans, is refused.

**Revocation is judged at arrival**, never by the envelope's `issued_at`, which the
signer chooses. Answers already applied stay valid history. Gates name the person,
not a key, so revoking a key strands no gate.

#### 3.3 What the hub checks, and how it recovers

Before any state changes: the transport credential and its route allowlist; the
envelope schema; the envelope names **this** hub's id; the key is enrolled for the
signer and not revoked now; the signature; `issued_at` within its window; the nonce;
the signer is the gate's named human; the item is answerable at this `attempt` and
`epoch`. Then it attests with `submitted_by` = the person, and **the attestation
carries the envelope, its `key_id` and `nonce`, and the transport actor (`via`)**.

The nonce table (registry database) and the attestation (work store) do not share a
transaction. The write order is nonce row (committed as `received`), then the
attestation, then the row as `applied` with its receipt in one registry
transaction. A retry whose row is still `received` and whose item is already
terminal or advanced is **success** when the item's attestation carries the same
`(key_id, nonce)`; otherwise it is the slot's normal refusal. When two envelopes
race for one slot, the loser's row is set `refused` with the winner's receipt in the
same path that refuses it.

**Idempotency matches `(key_id, nonce, canonical payload)`**, not signature bytes —
ECDSA re-signs the same payload with different bytes. A different payload under a
used nonce is `replayed`.

**First valid answer wins.** A different second answer is refused `not_terminal`
after a pass, or `stale` after a fail (a failing verdict advances the attempt).

**The transport** is a credential that reaches, by a per-actor route allowlist, only
the answer route and its own receipts. It cannot forge an answer; it carries one.

**What the hub can and cannot verify.** It can verify the signature, the enrolled
key, the binding to hub, item, attempt and epoch, freshness and uniqueness. It
cannot verify that a human, rather than malware on the person's machine, caused the
signature, or that the screen showed the bytes that were signed. Keeping the key
behind an operating-system presence check is optional hardening that raises the
cost of silent signing; it is not proof of intent.

This closes the known issue "attestations are not signed" **inside this hub, for
human-gate answers**. It does not by itself make cross-organisation attestation
real (see 3.2). Agent attestations stay as the known issue describes.

### 4. Instructions are ordinary work, and the whole body is signed

An instruction is a work item filed on the company hub, carried by an envelope of
its own type (`asop.instruction/1`) whose payload is **the entire work-create body**
the hub will file — gate, blockers, requirements and assignee included — or its
canonical digest with the body alongside. Nothing outside the signed body is read;
an unsigned field next to it refuses the request. The natural key must start with
`owner:`, and the person's side sends no metadata in the first version. The
confirmation the person sees is rendered from the canonical bytes, not from a
draft. A signed instruction is arbitrary work for the company's runner, bounded only
by the runner's own gates and the company's procedures; that is its blast radius.

### 5. Push comes later

Push notices are built after the first loop works: derived from vehicles, sent as a
state snapshot upserted latest per child, strict on the receiver, to an **explicit
target** — a company hub may also have its own parent.

### 6. Conformance fixes this loop depends on

- **Epoch numbering follows §7.2**: a run never transferred is epoch 0 and its first
  transfer epoch 1. The hub writes 1 and then current + 1, so every stored epoch is
  one high. One migration decrements the run owner's epoch, every history entry and
  every step's stamped epoch on every work-store backend. No "0 or 1" leniency: a
  fence with two right answers is not a fence.
- **Epoch on every attestation** (§7.2). The hub checks it on renew and report only.
- **`not_terminal`** for a second answer (§7.3); the hub answers
  `attestation_invalid`.
- **Signed queries on** (`ASOP_REQUIRE_SIGNED_QUERY=1`) on every hub in this
  topology.
- **Read credentials name no agent of their own choosing**: the agent a ready-work
  read is filtered by is derived from the credential on the server, never taken
  from the query.

### 7. Order

The envelope depends on the epoch and attempt fence, the generated hub id and a
per-actor route allowlist, so it is built after them and before push: fence and hub
id, route and read allowlist, envelope and the runner's signer, then the first loop,
then signed instructions, then push. The first tests decide whether the two-store
recovery holds — a crash between the attestation and the nonce update, and two
envelopes racing for one slot, on every backend — then cross-hub replay with default
ids, then the published vectors against a second signer, before any secure-enclave
work.

## Open

1. **Spec placement of the envelope.** Built hub-local first — the attestation
   record still names the person as `submitted_by`, so this is conformance-neutral —
   and proposed for asop-spec as a minimal additive change: an optional `envelope` on
   the attestation, the two payload schemas and their vectors, the signing rule,
   `attempt` on the attestation, and the refusal codes.
2. **Where public keys are published.** Detection of a substituted key rests, in the
   first version, on the person's own records and the hub's feed. A directory the
   person controls, readable by companies, is the candidate for when the spec field
   lands.
3. **Envelope age.** How long a queued envelope stays valid while a company hub is
   unreachable; a day is proposed, defensible because the attempt and epoch fence
   bound what a late envelope can do.
4. **Gates of a person removed from a company.** Removing the person strands every
   gate naming them, because no verb reassigns a verifier. A recorded administrative
   reassignment, parallel to run transfer (§7.2), is the candidate.
5. **Titles.** A tracker title can name a customer or an incident. The deterministic
   switchboard keeps it away from any model holding other companies' rows, not away
   from the screen.
6. **Presence check.** Optional in the first version.
7. **Identity across organisations** stays as 0007 §4 left it. The envelope needs no
   identity provider; a provider could later vouch for enrolled keys.

## Consequences

- **The first loop needs hub code**: the epoch migration and fence, the generated hub
  id, the route and read allowlist, and the envelope route with a key file, enrolment
  events, a nonce and receipt table and a migration on every storage backend. The
  envelope adds the `cryptography` library to the server install.
- **The personal machine is the root of trust.** The person's signing keys live
  there. Per-company keys keep a compromise revocable one company at a time.
- **A company hub can refuse or delay** an answer in its own state, and can enrol a
  key it controls; it cannot sign under the person's published key without that
  showing, and it cannot use an envelope on another company's hub.
- **The partition rule is a runner property.** The hub enforces who signed, which
  gate, which attempt and epoch, and what a credential may reach; it cannot see a
  model context, and says so.
- **Latency is the pulse cadence plus the poll interval.**
- **Timing and existence still leak** per company to whoever sees the switchboard
  (0006, open).

## Revisit condition

Revisit if a company needs to control what leaves its hub before push exists; if
the switchboard needs a model to be usable (that breaks the partition rule as
stated here); if the spec comes to permit a delegate to answer a human gate
(that reopens alternative (b)); or if a signed answer is ever accepted that its
signer did not intend and the presence check would not have stopped it (that would
mean the hardening is load-bearing and must stop being optional); or if a company
hub is ever found to have enrolled a key the person did not create (that would make
an external key directory a requirement rather than an open item).
