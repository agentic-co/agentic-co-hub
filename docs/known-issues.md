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
| **6b** | The HMAC covers the path but not the query string, so one captured signed `GET /events` replays as any feed query for the replay window. | Cannot be fixed server-side alone; both ends must change. |
| **A15** | The server signs the percent-**decoded** path while a client signs the wire form, so any path needing encoding fails as a 401 rather than as anything pointing at path handling. | The repo already chose a side: `auth.py`'s own bad-signature remediation tells callers to sign "the path exactly as sent". The server contradicts its own error message. |
| **MCP refusals** | The MCP encoding renders a `Refusal` as `ToolError(str(exc))`, so the machine `code` survives only as a string prefix. Over HTTP it is a field. | `errors.py` says clients branch on the code; over MCP they cannot without parsing prose. |


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
- **A14** — `auth.load_keys()` re-reads the key file from disk on every request when the app is constructed without explicit keys.
- **A16** — `resolve_file` on a FIFO blocks forever with no timeout, and `path.exists()` is true for a directory, so `file:/some/dir` raises an uncaught `IsADirectoryError`.

## Not defects, stated because they read like them

- **The adoption gate counts identities, not humans.** One person holding two
  keys counts twice and no mechanism here can tell. Letter case was fixed
  specifically because it is the variant that looks identical to whoever reads
  the report.
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
