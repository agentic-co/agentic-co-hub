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
| **6a** | A scope conflict raised by an unverified `holder` claim is indistinguishable from a verified one. `holder` is payload-supplied; the lease records `holderAttested`, but the conflict record third parties read does not carry it. | Needs a decision on whether the flag propagates or attested leases stop raising third-party conflicts. Both are honest fixes; the test accepts either. |
| **claim 3** | Seven scope-evasion routes: repo-name case, prefix case, zero-width characters, BOM, Unicode NFC/NFD, trailing dot, and `Scope()` bypassing validation. | Each makes two claims that overlap in reality fail to intersect. The repo-name one is worst — it hides a lease from the whole registry, and GitHub and ADO both treat `Acme/X` and `acme/x` as one repository. |
| **claim 5** | Four paths return HTTP 500 with "This is a registry bug" instead of a refusal — a non-numeric `ttlSeconds` or `limit`, an unreachable snapshot URI — and the generic handler echoes raw exception text, including filesystem paths, to the caller. | The fix is one coercion helper rather than four patches. Note the asymmetry that gives it away: on the same `GET /events`, a malformed `since` gets a careful refusal and a malformed `limit` gets "registry bug". |
| ~~**6b**~~ | ~~The HMAC covers the path but not the query string, so one captured signed `GET /events` replays as any feed query for the replay window.~~ **Fixed 2026-09-22**, with the transition still open by default. The signed string now carries a fifth line: the CANONICAL query — parsed, sorted, consistently escaped — which keeps the property the omission protected (a proxy reordering parameters must not invalidate a signature) while binding the values. Both ends had to change, so a legacy signature is still accepted until an operator sets `ASOP_REQUIRE_SIGNED_QUERY`; until they do, 6b remains open for whoever still signs the old string. `tests/test_replay_window.py` proves the replay, the reordering, and the switch. |
| ~~**A15**~~ | ~~The server signs the percent-decoded path while a client signs the wire form.~~ **Fixed 2026-09-22.** Settled the other way from the old remediation text: BOTH ends sign the DECODED path, and the remediation now says so. The server cannot reliably recover the wire form — ASGI's `raw_path` is optional and proxies rewrite it — while decoding is deterministic on both sides. Two encodings of one path denote one resource and should produce one signature; what must not happen is the ends disagreeing. |
| **MCP refusals** | The MCP encoding renders a `Refusal` as `ToolError(str(exc))`, so the machine `code` survives only as a string prefix. Over HTTP it is a field. | `errors.py` says clients branch on the code; over MCP they cannot without parsing prose. |
| ~~**F1**~~ | ~~JSONL `MemoryStore._record` wrote the item, then its audit row, with no shared transaction; a failure between them left an unaudited item and, after a retried revise, a permanent hole in the audit chain.~~ **Fixed 2026-10-31.** The audit row is written FIRST. The worst case left is an audit row for a version that never landed, and `_audits` reads past it (only versions the item reached, the last row per version). Both directions are tested in `tests/test_fault_injection.py`. |
| ~~**F2**~~ | ~~`Outbox.settle` removed the settled lines before appending their receipts, so a failure between them lost both.~~ **Fixed 2026-10-31.** Receipts first, then the lines go. The worst case left is a receipt for a line still in the outbox, which the next drain republishes and the hub dedupes on the line id — **true for `attest` and `work_report` only; see R4.** |
| ~~**F3**~~ | ~~After a process died mid-line, the next `Outbox.push` was glued onto the torn fragment and both were quarantined as one line.~~ **Fixed 2026-10-31.** Every append goes through `agentco/appendlog.py`, which starts a fresh line when the file does not end in one: the fragment stays its own quarantined line and the next record lands whole. |
| ~~**F4**~~ | ~~The same glue in the JSONL memory audit log and the credits ledger, where `_audits` skipped the glued row silently.~~ **Fixed 2026-10-31** by the same helper, which the write-back dead-letter log now uses too. |


## Open, from the 2026-11-07 review (R4-R21)

Found by a three-lane review of the work queue, auth/envelope, and storage
layers on 2026-11-07. R1-R3 from the same review were fixed in #87 before
this table existed. Every row below gets a strict-xfail test before its fix,
under the same conventions as `tests/test_adversarial_findings.py`; the tests
live in `tests/test_review_r*.py`, one file per lane.

| # | Defect | Lane |
|---|---|---|
| **R4** | **F2's "the hub dedupes on the line id" only holds for two of eight verbs.** `registry_publisher` passes `idempotency_key=line_id` for `attest` and `work_report` only. A drainer that dies after the receipt and before removing the line republishes `claim_scope` (a second lease), `snapshot`, `adjudicate`, `sop_revise` (a second version) and `sop_activate`. The hub has no idempotency key on those verbs at all, so the fix is hub-side: accept `idempotencyKey` and return the recorded outcome. `release_scope` is idempotent by state. | outbox |
| **R5** | **More 500s from wrong-typed payload fields, beyond claim 5.** On `POST /scope-claims`: an int `repo`, `holder` or prefix, or a non-string `intent`. On release: a dict/list `action` (`sqlite3.ProgrammingError`). On `/work/pull`: `capabilities: 5`. On `POST /work`: `requires: 5` or `metadata: [1]`. `ttlSeconds: Infinity` raises `OverflowError`, which `_int` does not catch. The handlers catch `WorkError`/`ValueError` only, so `TypeError`/`AttributeError` reach the "registry bug" handler, which echoes the exception text. | input |
| **R6** | **Claim TTL is unbounded.** `Queue.claim` and `/work/pull` never bound `ttl_seconds`; `renew` refuses `<= 0` and caps at `lease_max_age`. `ttl <= 0` returns "leased" on an already-expired lease. `ttl = 10**9` pins an item until 2058: `retire` refuses a live lease and `reap` takes only expired ones. | input |
| **R7** | **`report_result` honours an expired lease that `renew` refuses.** The fence checks attempt and holder, not expiry, so whether a late report lands depends on whether a reaper happened to run in the gap. | input |
| **R8** | **A retried `attest` drops its adjudication rider.** The verdict lands in one write and `adjudicate` runs as a second. If the second fails, the retry with the same `idempotencyKey` hits `_AlreadyAttested` and returns early, so the rider is never applied and the caller is told it succeeded. | input |
| **R9** | **Scope claim and release are check-then-act across separate transactions.** `leases.claim` reads live leases, then inserts, then writes each event in its own commit. Two processes claiming overlapping scope concurrently both see no conflict. A crash between commits leaves a lease no feed subscriber saw. `release`'s UPDATE has no `released_at IS NULL` guard, so concurrent releases both emit `ScopeReleased`. | outbox |
| **R10** | **Any authenticated actor can read the whole queue.** `?ready=1` is forced to the signer (ADR 0008 §6), but plain `GET /work` returns every item, and `GET /work/{id}` has no per-actor check. `authz.py`'s rationale reads as if non-operators were filtered. | auth |
| **R11** | **MCP local mode skips owner-party separation.** Only the HTTP app sets `queue.owners`, so over MCP-local `party_of` returns the actor and one person's two agents can execute and verify the same judged gate — which HTTP refuses. | auth |
| **R12** | **`Queue.party_of` falls back to the actor when `owners()` raises.** The comment says "stricter than nothing", but comparing actors is weaker than comparing parties: a malformed key table silently turns owner separation off. | auth |
| **R13** | **The request body is buffered in full before authentication.** `_handle` awaits `request.body()`, then checks `MAX_BODY_BYTES`, then authenticates. An anonymous client can force an unbounded buffer, and every failed-auth request writes a `calls` row. | auth |
| **R14** | **Envelope nonce rows are pruned before the envelope expires.** `submit` accepts `issued_at` up to `MAX_FUTURE_S` ahead and judges expiry from `issued_at`; `prune` deletes by arrival time. A future-dated envelope loses its nonce row up to 300 s early, and a refused or held envelope can be resubmitted in that gap. | auth |
| **R15** | **`clearance.actors_from_key_table` re-reads the key table on every request**, uncached — the A14 pattern, not stat-cached the way `auth` and `authz` are. | auth |
| **R16** | **Postgres: the change feed can lose events permanently.** `seq` comes from an identity column at INSERT and becomes visible at COMMIT, and commit order does not follow seq order. A reader that sees seq 6 before seq 5 commits moves its cursor past 5 for good. SQLite is safe because one writer lock orders commits; the pool removed the process-wide lock that did the same for Postgres. | postgres |
| **R17** | **Postgres: concurrent cold starts race the migrations.** `BEGIN IMMEDIATE` becomes a plain transaction, so two processes both pass the `already` check; the loser fails on a duplicate column or the ledger primary key. | postgres |
| **R18** | **Postgres: concurrent child creates under one parent lose `blocked_by` entries** and can exceed `MAX_SUBTASKS_PER_TASK`. The parent's read-modify-write is unlocked; the advisory lock covers natural keys only. A parent can then close with a child open. | postgres |
| **R19** | **Postgres: concurrent SOP authoring can destroy a committed version and reuse its number.** `SqlSopLibrary._write_all` deletes the whole table and re-inserts; `FOR UPDATE` cannot protect that under READ COMMITTED, nor a first create on an empty table. | postgres |
| **R20** | **JSONL `record_trace` writes the dedupe marker last.** States, then credits — and `_trace_seen` reads the credits ledger, so a crash between them credits a redelivered trace twice. The F1/F2 "guard first" order was not applied here. | postgres |
| **R21** | **Event cursor edges.** `v1:99999999999999999999` raises an uncaught `OverflowError` (500). `since=""` is "no cursor" to `decode_cursor` but not to the floor check, so after any prune it gets 410 `cursor_expired` instead of starting at the floor. | postgres |

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

## Finding 4: the plane fails open on an undeclared verifier registry

**Status: open, measured, and the standing argument for it does not hold.**

ASOP §5.3 and §9 both say an unset registry authenticates nobody. This plane
fails open: `policy.verifiers_from_env` treats an empty declaration as
UNDECLARED, `Queue.attest` never resolves `submitted_by` against the registry
at all, and authority rests on the transport (who is calling) plus capability
binding at claim. The runtime now fails closed, so the two disagree.

**The argument for failing open was checked and does not survive.** It reads:
a registry nobody is in "resolves every judged gate on the clock, which is work
approved on a timer". That conflates two independent mechanisms. The clock path
is `sweep_park_clocks` -> `resolve_by_default`, which never calls `attest`.
Refusing an unauthenticated attester cannot make the timer fire more often. The
timer hazard is real, and it is separately handled: `resolve_by_default` never
grants evidence, leaves `attestation` untouched, writes a "no check was run"
record, and `verifier_status` reports it in aggregate — which is what
`test_a_queue_approving_itself_on_a_timer_says_so_loudly` pins.

So failing closed is safe here. It is still a CONTRACT CHANGE rather than a
patch, which is why it has not simply been done.

**Measured cost**, from actually making the change and reverting it:

* authenticating `submitted_by` at `attest` fails 102 tests as-is;
* declaring a verifier set in the shared `queue`/`jsonl_queue` fixtures brings
  that to 23 distinct tests across 6 files;
* the remainder are not one shape — `test_verifier_binding` (6) is mostly
  fixture collision, since those tests assert on an UNDECLARED registry and
  must build their own queue; `test_adjudication` (6), `test_asop_v3` (5),
  `test_outbox` (2) and `test_conformance` (2) are genuine expectation changes
  reaching all three transports and the conformance harness itself.

**One thing NOT to do, learned by doing it.** Refusing to FILE a judged or human
gate with `on_timeout: pass` while no registry is declared looks like the tidy
companion fix. It is not: it deletes a configuration this plane supports
deliberately and tests on purpose (`test_a_clock_only_queue_never_reads_as_
configured`, `test_a_queue_approving_itself_on_a_timer_says_so_loudly`). The
design here is detect-and-report-in-aggregate, not prevent. Changing that is a
separate product decision and should be argued on its own terms.

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
