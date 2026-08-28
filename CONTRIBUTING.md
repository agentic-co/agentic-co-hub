# Contributing

## The one rule that is enforced mechanically

**Nothing in this repository names a real person, a real company, or a real
deployment.** Not in code, not in comments, not in tests, not in docs.

This is not a style preference. AgentCo was extracted from a private implementation that
had grown up inside one company's context, and the reason that extraction was necessary
is that a design document about a named organisation had been written into the same repo
as the product. The seam has to be maintained, or it closes again.

The rule has a positive form that is easier to apply:

> **Anything that names a company is configuration, not code.**
>
> Anything that names a *person* is not publishable at all.

### What that means in practice

| Instead of | Write |
|---|---|
| `dev.azure.com/acmecorp` | a configured base URL |
| `hannah@acmecorp.com` | `someone@example.com` | <!-- leakguard: allow -->
| a real tenant / subscription GUID | `<TENANT_ID>`, read from config |
| `/Users/yourname/Code/thing` | `$HOME`, `~`, or a configured path | <!-- leakguard: allow -->
| "Dana would read this as…" (a real colleague) | "a QA lead may read this as…" — and if the passage *analyses* a specific person, cut it rather than relabel it |
| an internal product codename | the capability, described generically |

Azure DevOps, Jira, Slack and Teams are products thousands of organisations run — naming
them is fine, and connectors for them belong here. What does not belong is *your* org
slug, *your* project names, *your* ticket numbers.

### It is checked, not trusted

`tools/leakguard` runs in CI and as a pre-commit hook. It refuses a commit carrying a
real email address, a UUID, a home-directory path, an internal hostname, a credential
shape, or anything on the configured denylist.

```bash
python3 tools/leakguard/leakguard.py            # whole tree
python3 tools/leakguard/leakguard.py --staged   # pre-commit mode
```

Install the hook:

```bash
ln -s ../../tools/leakguard/pre-commit .git/hooks/pre-commit
```

A policy without a mechanism fails. This is the mechanism.

**False positives** are expected. Suppress with `leakguard: allow` in the line's comment,
or add the path under `[allow] paths` in `leakguard.toml`. Both are visible on purpose — a
suppression nobody can see is how a scanner quietly stops scanning.

**The denylist is configuration.** `leakguard.toml` is not committed here, because a
denylist of real names is itself a list of real names. Copy `leakguard.toml.example`,
keep yours local, and have CI supply its own.

## Code

- Python ≥ 3.11, standard library first. A dependency has to earn its place.
- **Fail loudly at the layer where the failure occurs.** No silent degradation, no
  swallowed exception, no watermark that advances on a partial success.
- **Every refusal carries a machine code and a remediation sentence.** A refusal that
  says only `scope_too_broad` teaches the caller to stop calling.
- Unreported values are `null`, never `0`. A route that reported nothing must never read
  as a route that spent nothing.

## Tests

`uv run pytest`. No network, no credentials — the suite is hermetic by construction and
must stay that way.

Write the test that would fail if the claim were false, not the one that confirms the
happy path. Two examples from the existing suite, both of which caught real defects:

- Snapshots are asserted to store no document body by scanning the database file's **raw
  bytes** for content that was definitely in the artifact — not by checking the API.
- A time-dependent test pins its own clock. A test that passes today and fails in a
  fortnight is worse than no test, because it gets relabelled "flaky" and ignored.

## Design changes

Load-bearing decisions live in `docs/decisions/` with their alternatives, the reasoning,
and — required — a **revisit condition**. A decision with no revisit condition is
doctrine, and doctrine is how a small system becomes an unarguable one.
