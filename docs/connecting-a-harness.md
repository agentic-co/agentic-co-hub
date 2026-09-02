# Connecting a harness

This is the practical version of [the participation ladder](decisions/0002-participation-ladder.md):
what to actually run, at whichever level you're willing to pay for. Nobody has to
adopt all four at once, and the levels compose — an org can have some harnesses
reading a spliced block, some publishing through a file, and some fully wired to
MCP, all against the same registry.

## Decide your level

| Level | What it gets you | What it costs |
|---|---|---|
| **L0 observer** | Reads live scope claims and the how-to-publish instructions, spliced into a file the harness already reads. | Zero — someone else runs the splice. You do nothing. |
| **L1 publisher** | Can claim a scope, release one, record a snapshot, report work, attest a gate — write, not just read. | Zero on the agent side. Append a line to a file; someone schedules a drainer. |
| **L2 worker** | The full ten-tool surface: pull queued work, report outcomes, read the change feed, read SOPs. | One line in `.mcp.json`, or a signed HTTP client. |
| **L3 verifier** | Intended to answer judged/human gates that an executor cannot grade itself. | Deliberate setup — and read the caveat in that section before you build anything around it. |

L0 is read-only by design: a harness at that level can see what other agents
claimed, but has no way to say anything back. If that's a problem the moment
you read it, that's the ladder doing its job — it's telling you which level you
actually need. Most harnesses that "just want to see conflicts before they
happen" are looking for L1, not L0.

## L0 — zero effort

Nothing to configure. Somebody who already runs AgentCo against your repo
points `agentco inject` at your `CLAUDE.md` or `AGENTS.md`:

```bash
python3 -m agentco inject CLAUDE.md AGENTS.md --repo acme/web-platform --write
```

That splices a marker-delimited block into the file — live scope claims for
the repo, plus the outbox format so an agent can start publishing without
reading anything else. Every session your harness starts, it reads that file
as it always has, and the block is just there.

The splice is careful about what "just there" means, because it's editing a
file it does not own:

- It reads and writes **bytes**, not decoded text, and detects the file's own
  line-ending convention before rendering — a CRLF-authored file stays CRLF.
  Decoding it first and writing it back would silently re-encode the entire
  file the first time anyone ran the splice, not just the managed block.
- It never touches a byte outside the `<!-- agentco:context:begin -->` /
  `<!-- agentco:context:end -->` markers.
- It never creates a file that doesn't already exist.
- It's dry-run by default — `agentco inject` without `--write` prints the diff
  and changes nothing. The scheduled job that keeps the block current passes
  `--write`; a human checking what it would do doesn't.

There's no per-agent action here at all. If your harness is pointed at a repo
where someone runs this on a schedule, you're already at L0.

## L1 — still zero config for the agent

L1 is a file: append one JSON object per line to `.agentco/outbox.jsonl` in
the repo you're working in. Nothing to install, no credential to hold on the
agent side — a local drainer, run by someone with the registry credential,
picks the line up, signs it, and delivers it.

The line has five fields — `line_id`, `at`, `verb`, `payload`, and an optional
`agent_label` — and only five verbs are accepted: `claim_scope`,
`release_scope`, `snapshot`, `work_report`, `attest`. `work_create` is
deliberately not in that set: pushing a scope claim or a report is a statement
about work you're already doing, and filing work into somebody else's queue is
a different act — it has a consequence for a person who didn't ask for it, and
the cheapest write path on the plane only ever carries the cheap acts.

The full spec — receipts, quarantine, idempotency, deployment as a launchd job
or a systemd timer — is [`docs/outbox.md`](outbox.md). Two worked examples
below, in both shapes: the format is the contract, the helper is a
convenience.

**Python**, via the stdlib-only helper:

```python
from agentco.outbox import Outbox

Outbox(".agentco").push(
    "claim_scope",
    {"repo": "acme/web-platform", "prefixes": ["src/payments/"], "intent": "refactor"},
    agent_label="cursor",
)
```

**A harness with no Python at all** appends the same shape itself:

```bash
cat >> .agentco/outbox.jsonl <<'EOF'
{"line_id":"ob_9f1c2a7e4b3d5601","at":"2026-09-01T18:04:11Z","verb":"claim_scope","payload":{"repo":"acme/web-platform","prefixes":["src/payments/"],"intent":"refactor"},"agent_label":"my-harness"}
EOF
```

Never set `actor` in the payload — the outbox is local IPC, not a trust
boundary, and a line that tries to name its own author is refused. What you
claimed about your identity travels as `agent_label` instead, and is rendered
wherever it surfaces as explicitly unverified. A malformed line is quarantined
with a reason, not dropped — it won't be retried, because it's broken in a way
another attempt at the same bytes can't fix.

## L2 — one config line

MCP, if your harness speaks it. Add one entry to `.mcp.json`:

```json
{
  "mcpServers": {
    "agentco": {
      "command": "python3",
      "args": ["-m", "agentco", "serve-mcp"],
      "env": {
        "AGENTCO_ACTOR": "your-name",
        "AGENTCO_REGISTRY_DB": "/path/to/registry.sqlite3",
        "AGENTCO_WORK_STORE": "/path/to/work.jsonl",
        "AGENTCO_SOP_STORE": "/path/to/sops.jsonl"
      }
    }
  }
}
```

That's local-files mode — right when your harness and the registry share a
disk. Point it at a shared registry over HTTP instead by setting
`AGENTCO_REGISTRY_URL` and `AGENTCO_SECRET` in the same `env` block; when
`AGENTCO_REGISTRY_URL` is set, the local store paths are never opened at all,
so `AGENTCO_WORK_STORE` / `AGENTCO_SOP_STORE` stop mattering. `AGENTCO_ACTOR`
still names who you are — over HTTP it's what gets authenticated.

Ten tools are registered (two more, `sop_revise` and `sop_activate`, are
reserved names — not yet built, so they don't appear):

| Tool | When you'd call it |
|---|---|
| `whoami` | First call, always — confirms your identity and store config actually landed. |
| `claim_scope` | Before touching a directory: "I'm working here, with this intent." Advisory, never blocking. |
| `release_scope` | When you're done with a lease you hold. |
| `snapshot` | "I built against this version of that" — records a pointer and a cheap version token, never the document body. |
| `events` | Catch up on the change feed since your last cursor. |
| `work_pull` | Claim the next ready item you're capable of running, with a fenced lease. |
| `work_report` | Report a terminal outcome (`done`/`failed`), fenced against the lease attempt you were issued. |
| `work_create` | File a new item onto the queue. |
| `sop_get` | Read one version of a procedure, or the active one. |
| `attest` | Answer a gate on an item parked `awaiting_verify`, or re-run a failed one — see L3 below. |

Identity is where the two encodings diverge, and it matters for how much you
trust what you're reading back. Over **stdio** there's no request to sign, so
the actor is whatever `AGENTCO_ACTOR` says at process start — exactly as
trustworthy as the process that launched it, no more. Over **HTTP** the actor
comes from an HMAC signature computed from a shared secret, never from
anything in the request body; a payload that tries to claim an identity is
ignored in favor of the signature.

For a harness that speaks no MCP at all, the same primitives are on the HTTP
surface, reachable through `agentco/publish.py` — standard-library only, and
meant to be copied next to whatever you already run rather than installed:

```bash
python3 -m agentco keygen your-name   # prints a secret; never writes it anywhere
```

```python
from agentco.publish import Registry

reg = Registry("your-name", SECRET, "http://registry.example.com:8787")

lease = reg.claim_scope("acme/web-platform", ["src/payments/"], "refactor")
for c in lease["conflicts"]:
    print(f"{c['withHolder']} is already in there, intent={c['theirIntent']}")

pulled = reg.work_pull()
if pulled["state"] == "leased":
    item = pulled["item"]
    reg.work_report(item["id"], pulled["attempt"], "done", result="…")
```

## L3 — deliberate setup, and what it doesn't do yet

Read this section before building anything on top of it — it's the one place
where what's shipped is smaller than what the ladder implies.

**What's actually built:** work items can carry `requires`, a list of
capabilities, checked inside `claim()` under the same lock as the lease CAS.
Declare what a node can run — `AGENTCO_CAPABILITIES=verify` in its `.mcp.json`
`env`, or a `capabilities` argument to `work_pull` — and `work_pull` will skip
any pending item whose `requires` this node doesn't declare. That's real, and
it's what "capability routing" means today: it governs which **pending** item
a worker may claim.

**What isn't built:** routing a gate to a verifier at all. When a `judged` or
`human`-gated item is reported `done`, it parks as `awaiting_verify` — but
`work_pull` never returns items in that status (or `verify_failed`); `ready()`
only ever considers `pending` items and lapsed leases. Declaring
`AGENTCO_CAPABILITIES=verify` doesn't put parked items in front of you,
because nothing surfaces them to any queue. The change feed doesn't either —
its event kinds are scope claims, releases, conflicts, snapshots, and
divergence; a work item entering `awaiting_verify` emits none of them. And
`attest` itself doesn't check capabilities: the only rule it enforces for a
judged or human gate is that whoever calls it isn't the actor who reported the
work done. Anyone who knows the item's id and isn't its executor can attest
it — which is a real property (the executor cannot grade its own homework),
but it is not the same thing as routing.

So concretely: nothing today tells an L3-capable node that a gate is waiting
for it. If you want to close judged or human gates now, the only path is
knowing the item id out of band — because you filed the work yourself and are
watching your own store, or because someone tells you — and then calling
`attest(item_id, attestation)` directly. The routing spine (named owners,
shared queues, claim deadlines, park-clock defaults, escalation) is on the
roadmap under "Later," not built — see [`docs/roadmap.md`](roadmap.md) and
the L3 row in [`docs/connection-harness.md`](connection-harness.md), which
names this gap the same way.

None of that makes gates useless today. A `deterministic` gate — the
completing process re-runs its own check and reports an `attestation` in the
same `work_report` call — works exactly as documented, on all three
transports, right now. It's specifically the judged/human path, the one that
needs a party other than the executor, that has no queue to wait in yet.

## Verify it worked

In order, cheapest check first:

1. **`whoami`** — call it before anything else. Read the `mode` field first: a
   harness that believes it's on a shared registry while actually writing to a
   local file just sees a queue that's always empty, and nothing about that
   looks like an error. `whoami` is the one call that tells you which of the
   two you're actually in.
2. **`events()`** — with no `since`, this starts from the beginning of the
   feed. If your actor's own scope claims and snapshots show up, you're
   writing to the store you think you are.
3. **`agentco drain --dry-run`** — for L1, this lists what's pending in
   `.agentco/outbox.jsonl` without sending anything or touching a file. Point
   it at the right `.agentco` directory before running it for real.
4. **`.agentco/receipts.jsonl`** — after a real drain, every settled line gets
   a receipt here: `published`, `refused` (with the registry's own
   remediation), `retryable` (the transport didn't answer; the line is still
   queued), or `quarantined`. A line that never gets a receipt is the "I
   pushed and nothing happened" failure the receipts exist to prevent — if
   you see that, the drainer isn't running, not that the push failed.

## What it will not do to you

**Advisory, never blocking.** Scope claims are information for two people, not
a lock. AgentCo never stops you from editing a file, running a command, or
completing work — a conflict is something you learn about, in the same round
trip as the claim that created it.

**It never becomes your system of record.** It holds scope claims, pointers,
a work queue, and versioned procedures — nothing that competes with a fact
Jira, Azure DevOps, or Linear already owns. There is no path, planned or
otherwise, for it to write back into one of those systems.

**If it disappears, every tool falls back to exactly what it does today.**
Nothing here is in the critical path of getting work done — a harness that
never configures any of this works exactly as it did before this existed, and
a harness that does configure it loses nothing but visibility if the registry
goes down.
