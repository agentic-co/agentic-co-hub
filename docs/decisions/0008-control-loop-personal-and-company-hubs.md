# 0008 — The owner's control loop: pull first, one partition per company, answers as signed envelopes

**Date:** 2026-10-18 · **Status:** accepted 2026-10-19 (every open point decided by the owner) · **Supersedes:** nothing; narrows
[`0007-hub-network-graph-and-channels.md`](0007-hub-network-graph-and-channels.md)
for one topology and amends its direction table for the case where the hub that
owns the work also executes it (§2)

> A decision with no revisit condition is doctrine. Every record here carries one.

Nothing in this record is built. 0005's push-only digest is still the only
hub-to-hub path that exists.

**Decided:** the topology and pull first (owner decision 2026-10-18, option C),
and every point this record left open (owner, 2026-10-19; Decisions 1 to 7 at the
end). Nothing is built yet; the build follows §7's order.

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
4. **Any key on a hub was full access**, the attestation path did not check the
   ownership epoch that §7.2 requires on every attestation, and a hub's id
   defaulted to the same string on every hub. Since this record was written the
   epoch is checked on every attestation and counted as §7.2 counts it (#54,
   #56), and a generated hub id and an attempt fence exist (#58); the route
   allowlist is §8's B2.

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
key; `title` the vehicle's subject title. **Titles are shown by default; a
per-company switch hides them**, and then that company's rows show the label and the
age only (Decision 5).

The **switchboard is deterministic and holds no model credential and no company
credential.** On selection it passes `ref` to the partition named by `label`.
Drill-in is a live read by that partition from its own hub. The personal hub holds
no company content and is not on the loop. Content from different companies meets
only in this deterministic display, never in a model context; what it shows is not
nothing: `ref` exposes item ids and failure counts.

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

**Where it is defined (Decision 1 = A, owner 2026-10-19).** The envelope is
defined in this hub, in its documentation and its code, and that definition is
the **v1 contract**: payload, signing rule, test vectors and refusal codes as
stated here. It is to be proposed to the ASOP spec for **0.7.0, as an additive
change with no change to existing 0.6.x semantics**: an 0.6.x store or client
that never sends an envelope behaves exactly as it does today, and the
attestation record keeps naming the person as `submitted_by`. Until the spec
adopts it, the envelope is hub-local and conformance-neutral; if the spec
adopts it with changes, the hub follows the spec and states the migration.

**What is signed.** A payload of `type` (`asop.answer/1`), `hub` (the owning
company hub's id, §3.1), `item` (the original item, §8 S1), `verify_attempt`, `stage`,
`epoch` (0 outside a run, §8 S2), `verdict` (`{passed,
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
**Envelopes cannot be enabled while no generated id is persisted.** The id is a
**new field beside the configured name**, not a replacement for it: the configured
name (`AGENTCO_HUB_ID`, default `local`) is also the run-ownership store name and
the authority that memory items carry, and replacing it would make every run
`not_the_owner` and every memory item a replica. A registry restored from backup
onto a second machine carries the same id; restoring a hub as a second, live hub
regenerates the id (and re-enrols its keys) rather than running two hubs under one
id (§8, S10).

#### 3.2 Keys, and what they do and do not give

Ed25519 by default; ECDSA P-256 (low-S) allowed so that keys in a platform secure
enclave or a hardware token can sign; the algorithm is bound to the enrolled key.
The company hub holds only the **public** key per declared human. A per-human HMAC
secret was rejected: the hub would hold the same secret and could write the
person's verdict, which is fact 3.

Stated exactly: **the company hub cannot produce a signature that verifies under the
key the person published. It owns the key directory, so it can enrol a key it
controls. A substitution is detectable only by someone who holds the person's key
fingerprints AND can read the hub's records independently** — the hub filters its
feed per reader, so the person's own view is not independent. Until 0.7.0's
directory, substitution is prevented only by the operator's honesty. The person keeps
every envelope they signed and the fingerprint of every key they published; every
enrolment and revocation is an event on the hub's feed (`HumanKeyEnrolled`,
`HumanKeyRevoked`), and the partition compares the keys the hub reports for the
person with its own. True non-repudiation needs two things this record does not
build: the envelope in the attestation record by the spec (Decision 1, 0.7.0), and a
public-key directory the hub does not control.

**One key per (person, company hub)**, generated by that hub's partition and never
reused for another hub, enrolled by that hub's operator from a public key the
person hands over out of band, fingerprint compared on a second channel. Never over
the parent; there is no enrolment verb on the wire. A key for a non-human actor, or
one public key under two humans, is refused.

**Where keys are held (Decision 2).** Each company hub holds the public keys in its
own records; every enrolment and revocation is an event on that hub's feed. After
spec 0.7.0 they move to a directory the owner controls.

**Revocation is judged at arrival**, never by the envelope's `issued_at`, which the
signer chooses: an envelope signed with a key revoked by the time it arrives is
refused. Answers already applied stay valid history. Gates name the person, not a
key, so revoking a key strands no gate.

**A person removed from a company (Decision 4).** Their identity stays on the record
(history keeps naming who answered); their keys are revoked at once, so any envelope
they signed and that arrives after is refused; and an administrator reassigns each
of their open gates through a recorded administrative transfer, parallel to the run
transfer of §7.2. Automatic reassignment waits for spec 0.7.0.

#### 3.3 What the hub checks, and how it recovers

Before any state changes: the transport credential and its route allowlist; the
envelope schema; the envelope names **this** hub's id; the key is enrolled for the
signer and not revoked now; the signature; the envelope's age (below); the nonce;
the signer is the gate's named human; the item is answerable at this `verify_attempt` and
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

**Age of a queued envelope (Decision 3).** Measured at arrival from `issued_at`,
against two windows a company hub configures: **fresh** (default 24 h) and a
**grace** window (default 50% of fresh, so up to 36 h).

- Fresh: applied, through every other check above.
- In the grace window: **neither applied nor refused.** The gate shows "needs your
  validation" and keeps its slot; the owner confirms by signing a **fresh envelope**
  for it. An unsigned yes is not accepted, so a late answer never lands on a click.
- Older than fresh + grace: refused.

Revocation is still judged at arrival, whatever the age.

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
cost of silent signing; it is not proof of intent. **In v1 it is optional and off
by default; when switched on, a failed check warns and never blocks answering**
(Decision 6).

This closes the known issue "attestations are not signed" **inside this hub, for
human-gate answers, only once the unsigned path is closed for the same gates**
(§8, B1): while `POST /work/{id}/attest` accepts `submitted_by` equal to the named
person over HMAC, and the company hub holds that person's HMAC secret, the hub
can still write the person's verdict without an envelope. It does not by itself
make cross-organisation attestation real (see 3.2). Agent attestations stay as
the known issue describes.

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

**v1 is pull only, with a short poll interval** (Decision 7). Push notices are
built after the v1 milestone: derived from vehicles, sent as a
state snapshot upserted latest per child, strict on the receiver, to an **explicit
target** — a company hub may also have its own parent.

### 6. Conformance fixes this loop depends on

- **Epoch numbering follows §7.2**: a run never transferred is epoch 0 and its first
  transfer epoch 1. The hub writes 1 and then current + 1, so every stored epoch is
  one high. One migration decrements the run owner's epoch, every history entry and
  every step's stamped epoch on every work-store backend. No "0 or 1" leniency: a
  fence with two right answers is not a fence.
- **Epoch on every attestation** (§7.2) — done (#54); the count above — done (#56).
- **`not_terminal`** for a second answer (§7.3) — done (#58).
- **Signed queries on** (`ASOP_REQUIRE_SIGNED_QUERY=1`) on every hub in this
  topology.
- **Read credentials name no agent of their own choosing**: the agent a ready-work
  read is filtered by is derived from the credential on the server, never taken
  from the query; for the partition's credential that is the credential's PARTY
  (§8, B2).

### 7. Order

The envelope depends on the epoch and attempt fence, the generated hub id and a
per-actor route allowlist, so it is built after them and before push: fence and hub
id (built), route and read allowlist with the partition credential's route set and
the routes it needs (§8, B2: the hub-id route, the single-item read, the
party-filtered ready read, the HumanKey-only feed read); then the operator's enrol
and revoke verbs with their `HumanKey*` events; the envelope, its nonce table and
nonce GC, `envelope_required` (B1) and the runner's signer; the grace state and its
confirmation flow (S4); the gate-reassignment verb Decision 4 needs (nothing
reassigns a verifier today); then the first loop, then signed instructions, then
push. The first tests decide whether the two-store
recovery holds — a crash between the attestation and the nonce update, and two
envelopes racing for one slot, on every backend — then cross-hub replay with default
ids, then the published vectors against a second signer, before any secure-enclave
work.

### 8. Rules added by the adversarial review (2026-10-19)

A read-only adversarial review of this record against the code on main found two
blockers and a set of underspecified points. No owner decision is overturned;
these are the rules the build follows. Each code citation was re-checked on main.

**B1. The unsigned path closes when the signed one opens.** (a) The partition's
transport credential is a distinct actor with `owner: <person>` in the key table,
never the person's own key. (b) Once a human has an enrolled key, an unsigned
`POST /work/{id}/attest` on a gate naming them is refused **`envelope_required`**;
until then the unsigned path behaves as today. (c) The stored attestation carries
the envelope (with `key_id`, `nonce` and the transport actor), or reads as
`legacy` when it was not signed.

**B2. The partition credential's routes, exactly.** A credential of this class
reaches only:

- `GET` the hub's generated id (a new route);
- `GET /work?ready=1`, filtered on the server to items assigned to the
  credential's **party** (`auth.owner_of`), never to unassigned items: today
  `ready(agent=X)` also returns every unassigned ready item;
- `GET /work/{id}` (a new single-item read) for that party's vehicles and the item
  each one `verifies`, and nothing else;
- `GET /events` restricted to `HumanKey*` kinds;
- `POST` the answer route;
- `GET` its own receipts.

Nothing else: not the unfiltered `GET /work`, which lists the whole queue.

**S1. What `item` and `attempt` name.** `item` is the vehicle's `verifies` id: the
answer targets the original item. The attempt field is named **`verify_attempt`**
and equals the item's `verify_failures` (a failing verdict advances it; a lease
does not), replacing the ambiguous `attempt`. The published vectors include one for
each field. The attempt fence built in #58 (`answer_attempt` = lease attempt plus
failing verdicts) is brought to this definition before the envelope uses it.

**S2. The epoch of an item outside a run is 0**, stated and checked equal to 0;
null is not a value of the field.

**S3. Time.** An envelope with `issued_at` more than 300 s after arrival (the
transport's `MAX_SKEW_S`) is refused **`envelope_future`**. Nonces are kept for at
least fresh + grace measured from arrival, then collected.

**S4. The grace confirmation.** It is a complete NEW `asop.answer/1`, whose verdict
the partition takes from its OWN stored envelope for `(item, verify_attempt)`.
With no local envelope for that slot the partition shows "the hub reports an
answer you did not send" and offers no confirm. The late envelope's nonce is
recorded `expired`, and nothing of its content touches the item.

**S5. Two-store recovery, precisely.** A nonce row `received` whose item is still
answerable at `(verify_attempt, epoch)` with the same canonical payload: resume
and attest. A row `received` whose item has advanced and carries the same
`(key_id, nonce)`: success. Anything else: the slot's normal refusal.

**S6.** The key-substitution wording in §3.2 is corrected above. The revisit
condition names who finds a substituted key.

**S7. `owner:` natural keys.** A `naturalKey` starting `owner:` is refused on every
route except the signed-instruction route: today a duplicate natural key returns
the EXISTING item as `accepted`, so an unsigned filing could pre-empt a signed
one. The instruction route's response echoes the canonical digest of what it filed.

**S8. `asop.instruction/1` carries the same header as `asop.answer/1`** (`hub`,
`signer`, `key_id`, `nonce`, `issued_at`); its `naturalKey` is mandatory.

**S9.** §7's order and §6's status are corrected above; the stale "renew and report
only" wording is gone.

**S10.** The generated id is a new field (§3.1, corrected above); the ownership
store name and memory authority keep the configured name.

**Notes.**
- N1: each partition reaches its hub over TLS with a pinned endpoint; HMAC
  authenticates requests, not the channel.
- N2: the display wording in §1 is corrected above.
- N3: partition labels are unique, checked at configuration load.
- N4: refusing a key for a non-human needs `ASOP_HUMANS`; the enrolled-key table
  is the registry for human gates.
- N5: the signed bytes are the type line, one `\n`, then the canonical JSON; the
  nonce is 32 lowercase hex characters; `check`, `exit_status` and `at` on the
  stored attestation are written by the hub and are outside the signature.
- N6: the person's machine is the root of trust. Malware on it holds every key,
  and a signed instruction may file UNGATED work: arbitrary runner execution on
  every company the person serves.

## Decided (formerly open; owner, 2026-10-19)

1. **Spec placement of the envelope — Decision 1 = A.** Defined in the hub now,
   docs and code, as the v1 contract (§3). To be proposed to asop-spec 0.7.0 as an
   additive change, with no change to existing 0.6.x semantics: an optional
   `envelope` on the attestation, the two payload schemas and their vectors, the
   signing rule, `verify_attempt` on the attestation, and the refusal codes. Hub-local and
   conformance-neutral until then, because the attestation record still names the
   person as `submitted_by`.
2. **Where public keys are held.** In each company hub's own records; enrolment and
   revocation are events on that hub's feed. After spec 0.7.0 they move to a
   directory the owner controls (§3.2).
3. **Envelope age.** Fresh up to 24 h by default, configurable per company hub, and
   applied. In a grace window after it (50% of fresh by default, so to 36 h, also
   configurable) it is neither applied nor refused: the gate shows "needs your
   validation" and the owner confirms by signing a fresh envelope; an unsigned yes
   is not accepted. Older is refused. Revocation is judged at arrival (§3.3).
4. **Gates of a person removed from a company.** Identity kept on the record; keys
   revoked immediately, so envelopes signed with a revoked key are refused; an
   administrator reassigns each open gate through a recorded administrative
   transfer. Automatic reassignment waits for spec 0.7.0 (§3.2).
5. **Titles.** Shown by default in the roll-up. A per-company switch hides them, and
   that company's rows then show the label and the age only. Drill-in reads the full
   content live from the company hub (§1).
6. **Presence check.** Optional in v1 and off by default. A failed check warns and
   never blocks answering (§3.3).
7. **Push notices.** After the v1 milestone; v1 is pull only with a short poll
   interval (§5).

**Identity across organisations** stays as 0007 §4 left it: the envelope needs no
identity provider, and a provider could later vouch for enrolled keys.

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
- **Latency is the pulse cadence plus the poll interval**, which is why v1's poll
  interval is short (Decision 7).
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
an external key directory a requirement rather than an open item) — found by the
person's partition comparing the keys the hub reports against its own fingerprints,
by an auditor reading the hub's database directly rather than its filtered feed, or
by a second company finding a key it did not enrol.
