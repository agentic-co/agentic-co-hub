# AgentCo

**A coordination layer for organisations running more than one agentic harness.**

Everyone on your team is running their own AI coding agent. Different tools, different
vendors, no two configured the same — and nobody is giving theirs up. Individually they
work fine. Together they are blind to each other:

- Two agents edit the same directory and nobody finds out until the merge.
- One builds against a spec that changed last Tuesday.
- An agent hits something it cannot decide, asks a human, and the question goes nowhere —
  because nothing guaranteed it was delivered.

AgentCo holds the three things nothing else holds: **the claims people and agents make
about each other's work**, **the pointers you built against, so you are told when they
move**, and **a router that puts a decision in front of a named human and records the
acknowledgement**.

## What it is not

**It is not your system of record.** Jira, Azure DevOps, Linear and GitHub Issues stay
authoritative. AgentCo never becomes the place work lives, and it holds no competing
version of a fact another system owns.

**It never blocks anyone.** Scope claims are advisory. If AgentCo is down, every tool
falls back to exactly what it does today.

**It does not replace your harness.** Claude Code, Cursor, Copilot, Aider, a bespoke
in-house agent — keep all of them. Integration is one config entry, and nothing about
how your harness works changes.

## Status

**Early, and usable.** The coordination primitives are implemented and tested: scope
claims with conflict detection, snapshot pointers with divergence delivered at a cadence
boundary, a resumable change feed, a work queue with a fenced lease protocol proven across
twelve real processes, versioned SOPs, an MCP surface, and both injection tiers. See
[`docs/`](docs/) for the design, [`docs/roadmap.md`](docs/roadmap.md) for what is built
versus planned, and [`docs/known-issues.md`](docs/known-issues.md) for what is broken.

The project's own adoption gate is written down and deliberately hard to game: **two
identities other than the author publishing weekly for four consecutive weeks.** Stars
are the vanity metric that will be available; weekly publishers are not. A public repo
with no users is a worse outcome than a private tool with two, because it looks like
adoption while being none.

## Design principles

These are load-bearing, and each was derived from a failure mode rather than from taste.

| Principle | Why |
|---|---|
| **Any tool may push. No tool may file.** | A push is a signal; the owning controller decides what becomes work — with a clock, because "I pushed and nothing happened" is the most adoption-lethal outcome available. |
| **Advisory, never blocking.** | Making concurrency visible is most of the value and none of the political cost — but only if it never stops anyone working. |
| **Pointers, never copies.** | Snapshots record a URI plus a cheap version token. No document body is ever stored. A second document store is how a coordination layer becomes a system of record by accident. |
| **Fail static.** | If the plane is down, nothing is blocked; it reconciles when it returns. |
| **Every refusal carries a remediation.** | A tool that refuses without saying what to do instead teaches people it is broken. *(Four error paths still return a bare 500 instead of a refusal — known, with failing tests against them.)* |
| **Anything naming a company is configuration, not code.** | Which is what makes this repo publishable at all — see [CONTRIBUTING](CONTRIBUTING.md). |

## Getting started

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/mabidoli/agentco && cd agentco
uv run --extra dev --extra server --extra mcp pytest -q
```

### Run it

Point it at a directory for its state, mint a secret for yourself, and start the
service:

```bash
export AGENTCO_REGISTRY_DB=~/.agentco/registry.sqlite3
export AGENTCO_WORK_STORE=~/.agentco/work.jsonl
export AGENTCO_REGISTRY_KEYS=~/.agentco/keys.json

python3 -m agentco keygen you > ~/.agentco/keys.json   # then chmod 600
python3 -m agentco serve --port 8787                   # loopback by default
```

### Publish something

`agentco/publish.py` is standard-library only and meant to be copied next to
whatever you already run. No install required on the calling side.

```python
from agentco.publish import Registry

reg = Registry("you", SECRET, "http://127.0.0.1:8787")

# "I am about to work in these directories." Advisory — it blocks nobody.
lease = reg.claim_scope("acme/web-platform", ["src/billing/invoices"], "implement")
for c in lease["conflicts"]:
    print(f"{c['withHolder']} is already in there, intent={c['theirIntent']}")

# "I am working from this version of that." The document is never fetched or stored.
reg.snapshot("git:/path/to/repo#main", "baseline for the redesign")

# Catch up on everything since your last cursor.
feed = reg.events()
```

### Connect a harness

One entry in your `.mcp.json`, and nothing about how your harness works changes:

```json
{
  "mcpServers": {
    "agentco": {
      "command": "python3",
      "args": ["-m", "agentco", "serve-mcp"],
      "env": {
        "AGENTCO_ACTOR": "you",
        "AGENTCO_REGISTRY_DB": "/path/to/registry.sqlite3",
        "AGENTCO_WORK_STORE": "/path/to/work.jsonl",
        "AGENTCO_SOP_STORE": "/path/to/sops.jsonl"
      }
    }
  }
}
```

Nine tools, and nine is a ceiling enforced by a test rather than remembered — a
large tool surface costs every calling harness context on every tool-choice
decision it makes.

### Reach a harness nobody configured

The above is pull-only: the model asks, AgentCo answers. To reach an agent whose
owner has done nothing at all, write into a file it already reads.

```bash
# Splice live scope claims into the repo's agent-context file. Dry run first.
python3 -m agentco inject CLAUDE.md AGENTS.md --repo acme/web-platform
python3 -m agentco inject CLAUDE.md AGENTS.md --repo acme/web-platform --write

# Or, per person rather than per repo: a session-start hook.
python3 -m agentco hook install ~/.claude/settings.json --write
```

`inject` writes a marker-delimited block and touches nothing outside it,
preserving the file's own line endings. `hook` takes a verbatim backup so
`uninstall` restores the original bytes exactly.

### Read the honest numbers

```bash
python3 -m agentco metrics        # weekly publishers, latency, conflict precision
python3 -m agentco gate1          # exit 0 iff the adoption gate is met
```

### Before you rely on it

Read [`docs/known-issues.md`](docs/known-issues.md). Twenty defects are open and
documented, each with a failing test naming the property that should hold. None
of them lose work or report a wrong result as a right one — those were found and
fixed. Being told what is broken is the point; a project this young that claims
none would be lying.

## Licence

[Apache License 2.0](LICENSE). Apache rather than MIT for the explicit patent grant,
which matters when the thing being adopted is infrastructure inside a company.
