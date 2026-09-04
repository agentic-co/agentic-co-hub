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
| **claim 1** | `resolve_https` sends HEAD, but urllib re-issues a redirect as GET, so a redirected pointer transfers the body it then discards. Nothing is stored either way. | Redirects are the norm for the document stores this targets. Fix is a redirect handler that preserves the method. |
| **claim 3** | Seven scope-evasion routes: repo-name case, prefix case, zero-width characters, BOM, Unicode NFC/NFD, trailing dot, and `Scope()` bypassing validation. | Each makes two claims that overlap in reality fail to intersect. The repo-name one is worst — it hides a lease from the whole registry, and GitHub and ADO both treat `Acme/X` and `acme/x` as one repository. |
| **claim 5** | Four paths return HTTP 500 with "This is a registry bug" instead of a refusal — a non-numeric `ttlSeconds` or `limit`, an unreachable snapshot URI — and the generic handler echoes raw exception text, including filesystem paths, to the caller. | The fix is one coercion helper rather than four patches. Note the asymmetry that gives it away: on the same `GET /events`, a malformed `since` gets a careful refusal and a malformed `limit` gets "registry bug". |
| **6b** | The HMAC covers the path but not the query string, so one captured signed `GET /events` replays as any feed query for the replay window. | Cannot be fixed server-side alone; both ends must change. |
| **A15** | The server signs the percent-**decoded** path while a client signs the wire form, so any path needing encoding fails as a 401 rather than as anything pointing at path handling. | The repo already chose a side: `auth.py`'s own bad-signature remediation tells callers to sign "the path exactly as sent". The server contradicts its own error message. |
| **MCP refusals** | The MCP encoding renders a `Refusal` as `ToolError(str(exc))`, so the machine `code` survives only as a string prefix. Over HTTP it is a field. | `errors.py` says clients branch on the code; over MCP they cannot without parsing prose. |

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
