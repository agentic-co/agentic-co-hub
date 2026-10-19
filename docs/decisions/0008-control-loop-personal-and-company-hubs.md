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
4. **Any key on a hub is full access today**, and the attestation path does not
   check the ownership epoch that §7.2 requires on every attestation.

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
transport credential that can do nothing else.

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
company hub's id), `item`, `attempt`, `stage`, `epoch`, `verdict` (`{passed,
reason}`, §5.3's shape), an optional `choice`, `signer`, `key_id`, a 128-bit
`nonce` and an integer `issued_at`. The bytes signed are the type line followed by
the payload serialized with the spec's **normative canonical JSON** (§3.10: keys by
code point, no whitespace, no floats) — the same function the spec package exports
for digests, not a second serialization.

**Asymmetric keys.** Ed25519 by default; ECDSA P-256 (low-S) allowed so that keys in
a platform secure enclave or a hardware token can sign. The company hub holds only
the **public** key per declared human. A per-human HMAC secret was rejected: the hub
would hold the same secret and could write the person's verdict, which is fact 3.

**One key per (person, company hub)**, enrolled by that hub's operator from a
public key the person hands over out of band, fingerprint compared on a second
channel. Never over the parent. A key for a non-human actor is refused.

**What the hub checks**, before any state changes: the transport credential; the
envelope names **this** hub; the key is enrolled for the signer and not revoked;
the signature; `issued_at` within its window; the nonce unseen; the signer is the
gate's named human; the item is still answerable at this `attempt` and `epoch`.
Then it attests with `submitted_by` = the person, and stores the envelope and the
transport actor (`via`) beside the attestation.

**First valid answer wins.** A duplicate of an applied envelope returns the
original receipt. A different second answer is refused `not_terminal` after a
pass, or `stale` after a fail (a failing verdict advances the attempt).

**The transport** is a channel-scoped credential that may post envelopes and read
its own receipts, and nothing else. It cannot forge an answer; it carries one.

**What the hub can and cannot verify.** It can verify the signature, the enrolled
key, the binding to hub, item, attempt and epoch, freshness and uniqueness. It
cannot verify that a human, rather than malware on the person's machine, caused the
signature. Keeping the key behind an operating-system presence check is optional
hardening that raises the cost of silent signing; it is not proof of intent, and
nothing here claims it is.

This closes the known issue "attestations are not signed" **for human-gate answers
carried by an envelope**. Agent attestations stay as the known issue describes.

### 4. Instructions are ordinary work, signed the same way

An instruction is a work item filed on the company hub with a natural key, under
the same envelope mechanism with its own type (`asop.instruction/1`), so its filer
is verifiably the person.

### 5. Push comes later

Push notices are built after the first loop works: derived from vehicles, sent as a
state snapshot upserted latest per child, strict on the receiver, to an **explicit
target** — a company hub may also have its own parent.

### 6. Conformance fixes this loop depends on

- **Epoch on every attestation** (§7.2). The hub checks it on renew and report only.
  The hub also numbers a never-transferred run epoch 1 where §7.2 says 0; settle
  this before epochs are signed.
- **`not_terminal`** for a second answer (§7.3); the hub answers
  `attestation_invalid`.
- **Signed queries on** (`ASOP_REQUIRE_SIGNED_QUERY=1`) on every hub in this
  topology.

### 7. Order

The envelope depends on the attempt and epoch fence and on narrow credentials, so
it is built after both and before push: fence, read-shape allowlist, envelope and
the runner's signer, then the first loop, then push.

## Open

1. **Spec placement of the envelope.** Built hub-local first — the attestation
   record is unchanged and `submitted_by` is still the person, so this is
   conformance-neutral — and proposed for asop-spec as a minimal additive field
   (an optional `envelope` on the attestation, the payload schema, the signing rule
   and the refusal codes), together with `attempt` on the attestation and the epoch
   base. Only the spec field makes a verdict checkable outside the hub that holds it.
2. **Envelope age.** How long a queued envelope stays valid while a company hub is
   unreachable; a day is proposed.
3. **Gates of a person removed from a company.** Revoking a key does not strand any
   gate — gates name the person, not a key. Removing the person does, because no verb
   reassigns a verifier. A recorded administrative reassignment, parallel to run
   transfer (§7.2), is the candidate.
4. **Titles.** A tracker title can name a customer or an incident. The deterministic
   switchboard keeps it away from any model holding other companies' rows, not away
   from the screen. Whether a company may replace titles with its own short label is
   open.
5. **Identity across organisations** stays as 0007 §4 left it. The envelope needs no
   identity provider; a provider could later vouch for the enrolled keys.

## Consequences

- **The first loop needs hub code**: the fence, the read projection and the
  envelope route, with a key file, a nonce and receipt table and a migration on
  every storage backend. The envelope adds one library dependency to the server
  install.
- **The personal machine is the root of trust.** The person's signing keys live
  there. Per-company keys keep a compromise revocable one company at a time, and the
  company hubs never hold anything that can sign for the person.
- **A company hub can still refuse or delay** an answer in its own state; it cannot
  forge one, and it cannot use an envelope on another company's hub.
- **The partition rule is a runner property.** The hub enforces who signed, which
  gate, which attempt and epoch, and what a credential may read; it cannot see a
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
mean the hardening is load-bearing and must stop being optional).
