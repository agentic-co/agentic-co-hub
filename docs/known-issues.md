# Known issues

Everything here is known, reproduced, and deliberately not yet fixed. Nothing
here loses work or reports a wrong result as a right one — those were fixed.

**Most of these have a failing test already.** `tests/test_adversarial_findings.py`
holds one test per defect, each named for the property that *should* hold rather
than for the bug, marked `xfail(strict=True)` so the suite stays green while the
defect stands and turns **red** the moment it is fixed with the marker left in.
Fixing one means deleting its marker in the same commit as the code.

That file's own docstring carries the conventions. Two are worth repeating here
because they are easy to get wrong:

- **A newly-passing test proves nothing until it has been run against the
  pre-fix code.** A test that could never have failed is not a regression test,
  and it looks identical in the summary line to one that could.
- **A test should accept any honest fix**, not the one its author would pick.
  Encoding a preference turns a test of the property into a test of a design.

---

## Open, with tests

| # | Defect | Why it is still open |
|---|---|---|
| ~~**6a**~~ | ~~A scope conflict raised by an unverified `holder` claim is indistinguishable from a verified one.~~ **Fixed 2026-11-09 (ADR 0011, decision 3: propagate).** The conflict record (`scope.find_conflicts`, so `POST /scope-claims`, `leases.conflicts_for` and the session hook) now carries `holderAttested` for the other lease, and the session block marks it `(attested, unverified)`. The conflict still fires; the flag is information for the reader. Additive on the wire. | |
| **claim 3** | Seven scope-evasion routes: repo-name case, prefix case, zero-width characters, BOM, Unicode NFC/NFD, trailing dot, and `Scope()` bypassing validation. | Each makes two claims that overlap in reality fail to intersect. The repo-name one is worst — it hides a lease from the whole registry, and GitHub and ADO both treat `Acme/X` and `acme/x` as one repository. |
| ~~**claim 5**~~ | ~~Four paths returned HTTP 500 with "This is a registry bug" instead of a refusal, and the generic handler echoed raw exception text.~~ **Fixed 2026-11-07** with R5: one coercion layer (`agentco/payload.py`); an unreachable snapshot URI is recorded unresolved; the generic handler logs the exception and returns a `correlationId`, never its text. | |
| ~~**6b**~~ | ~~The HMAC covers the path but not the query string, so one captured signed `GET /events` replays as any feed query for the replay window.~~ **Fixed 2026-09-22; required by default since 2026-11-09 (ADR 0011, decision 4).** The signed string carries a fifth line, the canonical query. A legacy signature on a request with a query is now refused `401` with no configuration. `ASOP_REQUIRE_SIGNED_QUERY` remains for one release as an opt-out: only an explicit `0`, `false`, `no` or `off` re-allows the legacy string, and any other value, a typo included, keeps it required. The variable goes with the legacy signature after 1.0. `tests/test_v1_wire_defaults.py` pins the default and the opt-out, `tests/test_replay_window.py` proves the replay, and `tests/test_signed_query_clients.py` proves every shipped client signs the query with the variable unset. | |
| ~~**A15**~~ | ~~The server signs the percent-decoded path while a client signs the wire form.~~ **Fixed 2026-09-22.** Settled the other way from the old remediation text: BOTH ends sign the DECODED path, and the remediation now says so. The server cannot reliably recover the wire form — ASGI's `raw_path` is optional and proxies rewrite it — while decoding is deterministic on both sides. Two encodings of one path denote one resource and should produce one signature; what must not happen is the ends disagreeing. |
| ~~**MCP refusals**~~ | ~~The MCP encoding renders a `Refusal` as `ToolError(str(exc))`, so the machine `code` survives only as a string prefix.~~ **Fixed 2026-11-09 (ADR 0004 option C).** A refusal over MCP is an error result whose `structuredContent` is the same `{state, code, message, remediation}` body HTTP returns, next to the unchanged `code: message — remediation` text. In process, the raised `RefusedToolError` carries `.code` and `.data`. Clients should branch on `structuredContent.code`; the text prefix stays for clients that parse it today. | |
| ~~**F1**~~ | ~~JSONL `MemoryStore._record` wrote the item, then its audit row, with no shared transaction; a failure between them left an unaudited item and, after a retried revise, a permanent hole in the audit chain.~~ **Fixed 2026-10-31.** The audit row is written FIRST. The worst case left is an audit row for a version that never landed, and `_audits` reads past it (only versions the item reached, the last row per version). Both directions are tested in `tests/test_fault_injection.py`. |
| ~~**F2**~~ | ~~`Outbox.settle` removed the settled lines before appending their receipts, so a failure between them lost both.~~ **Fixed 2026-10-31.** Receipts first, then the lines go. The worst case left is a receipt for a line still in the outbox, which the next drain republishes and the hub dedupes on the line id — for every verb since R4, with the residues listed there. |
| ~~**F3**~~ | ~~After a process died mid-line, the next `Outbox.push` was glued onto the torn fragment and both were quarantined as one line.~~ **Fixed 2026-10-31.** Every append goes through `agentco/appendlog.py`, which starts a fresh line when the file does not end in one: the fragment stays its own quarantined line and the next record lands whole. |
| ~~**F4**~~ | ~~The same glue in the JSONL memory audit log and the credits ledger, where `_audits` skipped the glued row silently.~~ **Fixed 2026-10-31** by the same helper, which the write-back dead-letter log now uses too. |


## Fixed: the 2026-11-07 review (R1-R21)

Found by a three-lane review of the work queue, auth/envelope, and storage
layers on 2026-11-07, and fixed the same day. R1-R3 shipped in #87. R4-R21
were recorded here first (commit `7ef24d2`), each got a strict-xfail test that
was run against the pre-fix code, and each fix deleted its marker. The tests
live in `tests/test_review_r_{auth,input,outbox,postgres}.py`; the Postgres
ones were proven live against Postgres 16 before their fixes.

| # | Defect | Fix |
|---|---|---|
| ~~**R4**~~ | ~~Outbox republish after a crash duplicated `claim_scope`, `snapshot`, `adjudicate`, `sop_revise`, `sop_activate`: only `attest` and `work_report` carried an idempotency key.~~ | All five take `idempotencyKey` on HTTP and the `Registry` client, and the drainer passes the line id. Keys live in `idempotency_keys` (migration 0018), scoped to the actor, newest 500 per verb; claim and snapshot record the key in the same transaction as the write. **MCP residue closed 2026-11-09** (ADR 0004 option C): `claim_scope`, `snapshot`, `sop_revise`, `sop_activate`, `attest` and the new `adjudicate` tool take `idempotency_key` over MCP, recorded the way HTTP records it, so a key replays the same answer whichever door the first call used. The MCP SDK silently drops arguments a tool does not declare, so a key sent to a hub older than this gives no protection and no error. **One residue, open:** on the JSONL SOP store `sop_revise`/`sop_activate` record the key just after the write, so a crash between the two can still repeat it. |
| ~~**R5**~~ | ~~Wrong-typed payload fields reached `TypeError`/`AttributeError`/`OverflowError` and came back as a 500 echoing the exception.~~ | One coercion layer, `agentco/payload.py`, at the HTTP boundary, with core checks in `leases`/`scope` so MCP refuses the same inputs. 400 naming the field. |
| ~~**R6**~~ | ~~Claim TTL unbounded: `<= 0` returned an already-expired lease, `10**9` pinned an item until 2058.~~ | `Queue.claim` refuses `<= 0` and clamps to `lease_max_age`, as `renew` already did. |
| ~~**R7**~~ | ~~`report_result` honoured an expired lease that `renew` refused.~~ | Resolved toward keeping work: the attempt fence is the authority and expiry only makes an item eligible for reclaim. `renew` now accepts a lapsed lease whose attempt is still current. A reclaim bumps the attempt, so the old holder is refused from then on. |
| ~~**R8**~~ | ~~A retried `attest` dropped its adjudication rider.~~ | A recognised retry applies the rider if the item has none yet. Adjudication is immutable, so it cannot apply twice. |
| ~~**R9**~~ | ~~Scope claim and release were check-then-act across separate commits.~~ | Conflict read, insert, events and conflict actions in one write transaction (`BEGIN IMMEDIATE` on SQLite, a per-repo advisory lock on Postgres). Release guards `released_at IS NULL` and emits only when it changed the row. |
| ~~**R10**~~ | ~~Any authenticated actor could read the whole queue through plain `GET /work` and `GET /work/{id}`.~~ | Non-operator, non-transport callers see items in reach of their party (assignee, holder, gate verifier or escalation, attester), plus unassigned items; out of reach answers like a missing id. The creator is recorded (reserved metadata key `filed_by`, stamped from the signer on `POST /work`, MCP `work_create` and every run step) and counts, so a filer keeps sight of what they filed; items filed before it existed keep the assignee rule. |
| ~~**R11**~~ | ~~MCP-local mode skipped owner-party separation.~~ | The local backend loads `queue.owners` from the key table, as HTTP does. |
| ~~**R12**~~ | ~~`party_of` fell back to the actor when the key table was unreadable.~~ | Fails closed: `503 owner_table_unreadable`. |
| ~~**R13**~~ | ~~The body was buffered in full before authentication.~~ | `Content-Length` checked first, bounded read, `413` over the limit, label parsed after auth. Failed-auth requests no longer write a `calls` row, so `/metrics` no longer counts them. |
| ~~**R14**~~ | ~~Envelope nonce rows were pruned before a future-dated envelope expired.~~ | Retention adds `MAX_FUTURE_S`. |
| ~~**R15**~~ | ~~The clearance key table was re-read on every request.~~ | Stat-cached on mtime and size, so an edit applies on the next call. |
| ~~**R16**~~ | ~~Postgres: the change feed could lose an event when commit order differed from seq order.~~ | Event inserts take a schema-keyed transaction advisory lock, so seq order equals commit order. Callers write the event last in their transaction; the lock is only held inside one. |
| ~~**R17**~~ | ~~Postgres: concurrent cold starts raced the migrations.~~ | A schema-keyed advisory lock per migration transaction, ledger re-checked under it, and the ledger table created under the lock too. |
| ~~**R18**~~ | ~~Postgres: concurrent child creates lost `blocked_by` entries and exceeded the child cap.~~ | The parent row is locked `FOR UPDATE` before counting and appending, after the natural-key lock, in that fixed order. |
| ~~**R19**~~ | ~~Postgres: concurrent SOP authoring could delete a committed version and reuse its number.~~ | The library lock is a schema-keyed advisory lock (covers the empty table), and `_write_all` updates and inserts per row instead of delete-all. `PRIMARY KEY (asop_id, version)` from migration 0009 was already the uniqueness guard. |
| ~~**R20**~~ | ~~JSONL `record_trace` wrote the dedupe marker last, so a crash double-credited a redelivered trace.~~ | Marker first. **Accepted trade-off:** a crash between the two writes now under-credits that one trace instead. Under-crediting is the direction the memory design already prefers. |
| ~~**R21**~~ | ~~A huge cursor raised `OverflowError`; `since=""` got `410` after a prune.~~ | Cursors outside 0..2^63-1 are `400 bad_cursor` (negative ones too, which used to get `410`); `since=""` means no cursor everywhere. |

Recorded and accepted rather than scheduled: signed non-envelope POSTs carry
no nonce, so a captured one replays for the 300 s window and harm depends on
the verb's own idempotency (R4 closes the outbox verbs); `appendlog` does not
fsync the directory after creating a file; the outbox quarantine keeps the
last 200 rows, not all of them; `settle` discards torn tails without
quarantining them.

## Removed: claim 1 (redirect downgraded HEAD to GET)

Fixed, and it had been fixed for a while — `_KeepMethodOnRedirect`
(`agentco/snapshots.py`) preserves the method across a 3xx, and
`test_registry.py` proves it by asserting the methods a real local server
RECEIVED through a redirect rather than the client's intent. The entry stayed
listed as open anyway.

Noted rather than quietly deleted, because a known-issues file that is wrong in
the SAFE direction is still wrong in the way that matters: it is what somebody
reaches for to decide whether a defect is already understood, and one stale row
teaches them not to trust the other rows. The same staleness hit this project's
conformance README in the same week.


## Open, no test yet

Small, cheap, and recorded here so they exist somewhere other than a chat log.

- **A13** — `_iso()` calls `astimezone(timezone.utc)` on possibly-naive datetimes, which Python treats as *local* time. A naive `now=` argument is silently shifted by the UTC offset.
- ~~**A14** — `auth.load_keys()` re-reads the key file from disk on every request when the app is constructed without explicit keys.~~ **Closed 2026-09-21.** Cached on the file's `(mtime_ns, size)`, so it is a `stat` per request rather than a read and a parse. Keyed on the file rather than on a clock deliberately: a TTL would make revocation eventual, and "how long until the key I just revoked stops working" would have no answer an operator could state. It is immediate, and `tests/test_revocation.py` asserts that on the next request with no restart.
- **A16** — `resolve_file` on a FIFO blocks forever with no timeout, and `path.exists()` is true for a directory, so `file:/some/dir` raises an uncaught `IsADirectoryError`.

## Not defects, stated because they read like them

- ~~**The adoption gate counts identities, not humans.**~~ **Fixed 2026-09-23.**
  It was listed here as something that reads like a defect and is not, on the
  grounds that "no mechanism here can tell". There is a mechanism now: the key
  table binds every agent actor to an owner, so `metrics.weekly_active_publishers`
  buckets by party and `gate1_status` resolves the operator to a party before
  excluding him. One person's three keys are one publisher; the operator's own
  agents no longer count as publishers other than the operator.

  Kept visible rather than deleted, because the entry was wrong in an
  instructive way. "No mechanism can tell" was true of the code and read as
  though it were true of the problem, which is how a defect gets filed as a
  property. The thing to notice is that it sat one line below the letter-case
  fix — the same defect, one level up, already solved once.
- **`k = 2`, the minimum scope depth, is unvalidated.** There is no usage data
  yet. The registry publishes its own conflict precision for exactly this, and
  the report refuses to recommend a change below a minimum sample.
- **`agentco/publish.py` contains a second copy of the signing function.** That
  is deliberate — the file exists to be copy-pasted by someone who never
  installs the package. It carries a `vendored-from` hash marker that goes
  stale, and fails a test, the moment `auth.sign` changes.

## Attestations are not signed, so cross-organisation attestation is not yet real

**Status: accepted for now, deliberately.** Recorded here so the capability is
not claimed by implication.

What exists is HMAC *request* signing (`agentco/auth.py`): a shared secret
covering method, path, timestamp and a body digest. It authenticates a request
in transit, and it is the right tool for that.

It is the wrong tool for attestation across a trust boundary, and the reason is
structural rather than a matter of key length. HMAC is symmetric. Both parties
hold the same secret, so neither can demonstrate to a **third** party who wrote
a given attestation — and either could have written one the other did not. The
stored attestation carries no signature field at all; it is a plain record of
check, exit status, environment, timestamp and verdict.

Within one operator's estate this is fine, and that is the deployment shape
today: the parties already trust each other, and the transport establishes who
is calling.

It stops being fine the moment the pitch is *multi-organisation* attestation —
which is the one position where this contract has ground nobody else is
standing on. Unsigned attestation across an organisational boundary is a shared
database with extra steps. Making it real needs per-participant asymmetric keys
and a signature over the attestation itself, so a verdict is checkable by
someone who trusts neither party.

**So do not describe cross-org attestation as supported.** The gap is the claim,
not the code: the code is honest about being an in-estate coordination plane.

## Finding 4: the plane failed open on an undeclared verifier registry

**Status: closed 2026-11-09 (ADR 0011, decision 2: fail closed in v1).**

`Queue.attest` now resolves a `judged` or `human` attestation's `submitted_by`
against the operator's declaration before accepting it (ASOP §5.3, §9): a route
in `ASOP_VERIFIERS` or a person in `ASOP_HUMANS` (`policy.may_verify`). Anyone
else is refused `unauthenticated`, HTTP 403. It is 403 and not 401 because the
transport did authenticate the caller, and a 401 sends a harness off to mint a
key. An unset registry authenticates nobody. Deterministic gates are unchanged.

The old argument for failing open ("a registry nobody is in resolves every
judged gate on the clock") confused two separate mechanisms: the clock path
(`sweep_park_clocks` -> `resolve_by_default`) never calls `attest`, so the
change cannot make a timer fire more often. Filing a judged or human gate with
`on_timeout: pass` on an undeclared hub is still allowed and still reported by
`verifier_status`; the design remains detect-and-report there, not prevent.
Claiming work stays self-asserted while nothing is declared. Signed human
answers (ADR 0008) are unaffected, because enrolment already requires a
declared human.

Measured at the change: 92 tests failed as-is; with a verifier list declared in
the shared fixtures, 17 distinct tests in 6 files remained. Each was resolved
deliberately, and the fix commit lists them.

**Operators upgrading must set `ASOP_VERIFIERS` (and `ASOP_HUMANS`), or every
judged and human gate waits for its clock.** See the upgrade section of
[`docker.md`](docker.md).

**Open question, not a defect:** when `ASOP_VERIFIERS` is set and a declared
human is not on it, that person is still refused on a judged gate
(`attestation_invalid`, from the capability check). This predates the change.
Spec §9 reads "it waits for a person", so whether a declared human may answer a
judged gate without also being listed as a verifier is undecided.

## Finding 5: snapshot admission is bypassed by a redirect

**Status: OPEN and KNOWN. HIGH. No test yet.** Recorded from internal review so a
reporter finding it independently can see it is not news.

`admission_reason` (`agentco/snapshots.py`) runs once, against the URI a
participant supplied, and resolves the hostname there to decide whether it is
internal. `resolve_https` then issues its HEAD request through
`_KeepMethodOnRedirect`, which preserves the method across a 3xx (that part is
Finding "claim 1", already fixed) but re-runs no admission check on the redirect
*target* — and the resolution done at admission is never bound to the
connection `urllib` eventually makes.

So a participant registers a public URL — passes admission cleanly — that 302s
to `127.0.0.1:<port>` or any other address `admission_reason` exists to refuse,
and gets back the internal HEAD request and its header oracle in response. This
is not one request: `check_all` re-resolves every live snapshot on a cadence for
the TTL (90 days by default), so one accepted registration keeps firing the
redirect on every scheduled re-check.

**Fix direction, not yet done:** re-run `admission_reason` against each redirect
target inside the handler rather than once at admission, and pin the resolved
address used for the connection rather than letting the redirect target be
re-resolved independently of what was checked.
