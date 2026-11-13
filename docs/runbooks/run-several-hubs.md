# Runbook: run several separate hubs

An operator with one hub per client engagement plus a personal one needs the hubs **walled from each
other**. Each hub is its own process with its own port, database, actor/key table, human and verifier
declarations and data directory. Nothing from one hub (data, credentials, keys, logs) is readable through
another, and one hub's credential is refused by every other. This runbook uses
[`tools/deploy/hubctl`](../../tools/deploy/hubctl) to make that the default and to prove it stays true.
Background: [ADR 0008](../decisions/0008-control-loop-personal-and-company-hubs.md) (personal and company
hubs), [`../docker.md`](../docker.md) (the single-hub container path).

Placeholders: `client-a`, `client-b`, `personal` (hub names), `hub.example` (a host name), `<actor>`
(an actor name), `<root>` (the instances root). Use your own values; never commit them.

## What `hubctl` gives you

```
<root>/<name>/            mode 0700   default root: ~/.agentco/hubs (expanded when you run it)
    hub.env               mode 0600   every setting this hub reads, and nothing from any other hub
    keys.json             mode 0600   this hub's actor -> secret table
    data/                 mode 0700   database, stores, outbox node dir, writeback files
    logs/hub.log                      this hub's log
```

| Command | What it does |
|---|---|
| `hubctl init <name> --port <port> [--backend sqlite\|postgres] [--pg-dsn ...] [--root <dir>]` | creates the instance; refuses a reused name, port or Postgres database; binds loopback unless you pass `--bind <addr> --expose-network` |
| `hubctl add-actor <name> <actor> [--verifier] [--human]` | mints that actor's credential with `agentco keygen`, prints it **once** to stdout, declares it in `ASOP_VERIFIERS` / `ASOP_HUMANS` |
| `hubctl service <name> [--print\|--install\|--uninstall]` | a launchd user agent (macOS) or systemd user unit (Linux) for that instance only; `--print` is the default and writes nothing |
| `hubctl status [<name>] [--json] [--conform [LEVEL]]` | up or down, port, backend, hub id, health (a signed `GET /hub` with the hub's own operator credential), optional conformance |
| `hubctl check-isolation` | exits non-zero, naming the violation, if any wall is breached |
| `hubctl backup <name> <dest>` | consistent copy of the database (SQLite online backup API) plus keys and env |
| `hubctl run <name>` | foreground run with only that instance's settings; what the service definition executes |

`--root` is accepted after the subcommand; `HUBCTL_ROOT` sets it for a shell. Run `hubctl` with the Python
that has this package installed (`uv run --extra server --extra envelope python tools/deploy/hubctl ...`).

## Layout A: two hubs on one machine

```sh
export HUBCTL_ROOT=~/.agentco/hubs
hubctl init client-a --port 8801
hubctl init personal --port 8802
```

Each gets its own port, SQLite file, key table and directories. The operator credential of each hub is
generated at `init` and lives only in that hub's `keys.json`; it is never printed and never equal to
another hub's.

**Declare humans and verifiers.** A hub with neither authenticates nobody at `attest`, by design: with
`ASOP_HUMANS` and `ASOP_VERIFIERS` both empty no route can answer a gate.

```sh
hubctl add-actor client-a <actor> --human        # a person who answers human gates; prints their credential once
hubctl add-actor client-a ci-verifier --verifier # an agent allowed to verify judged gates
```

Hand each credential to its owner over a channel you would send a password over, and never to anyone
for another hub. Declarations are read at start: **restart the hub** after `add-actor --human/--verifier`
(the key table itself is re-read on every request, so a new credential works at once and a revoked one
stops at once).

**Start them.** On a workstation, a service per hub:

```sh
hubctl service client-a --print                  # read it first: it names only client-a's files
hubctl service client-a --install
hubctl service personal --install
hubctl status                                    # both UP, different hub ids
hubctl check-isolation
```

`--install` writes `~/Library/LaunchAgents/com.agentco.hub.<name>.plist` (macOS, then `launchctl
bootstrap`) or `~/.config/systemd/user/agentco-hub-<name>.service` (Linux, then `systemctl --user enable
--now`). To try the files without touching the service manager, add `--unit-dir <scratch-dir>`. Services
restart on failure and log to `<root>/<name>/logs/hub.log`. The hub runs with a clean environment holding
only its own `hub.env`: an `AGENTCO_*` variable exported in your shell never reaches it.

`check-isolation` verifies, across every instance under the root: no shared port; no shared SQLite file
(including by hard link) or Postgres database; no shared key or secret value; no shared or nested data or
log directory, and every configured path inside its own instance; no two hubs with the same configured name
or the same generated hub id; modes private (root, instances, every file; no group or other bits; no
symlinks); and, for every pair of running hubs, that each credential of one is refused (401) by the other.
Run it after every change and from a timer.

## Layout B: hubs on different machines

Same commands, one hub per machine (or per group of machines): run `hubctl init` and `service --install`
on each. Each machine has its own root, so there is nothing to share and nothing to copy. Two things change:

- **Reachability.** A hub binds loopback. If another machine must reach it, use one of the two options
  below; do not bind a routable address with plain HTTP.
- **`check-isolation` only sees one machine's root.** Different machines cannot share a database or a
  directory, but they can still share a *secret* by accident (the same key table copied around). Compare
  fingerprints: `agentco keycheck <keys.json>` prints a per-secret fingerprint; no fingerprint may
  appear on two hubs.

### Behind a reverse proxy

Authentication is a per-actor shared secret over HTTP, so anything routable needs TLS in front. Keep the
hub on loopback, terminate TLS in a proxy on the same machine, and forward `https://hub.example` to
`127.0.0.1:<port>`. Give each hub its own host name and certificate (`client-a.hub.example`), never a path
prefix on a shared name: requests are signed over the path, and a prefix rewrite breaks signatures. Pass
the request method, path and query through unchanged. If you must bind a non-loopback address (for a
proxy on another host), `init --bind <addr> --expose-network` records that decision, and
`check-isolation` reminds you of it on every run.

### Over a private network

A WireGuard-style private network is equivalent: bind the hub to that interface's address (`--bind
<private-address> --expose-network`) and allow only the peers that need it. Without TLS, treat the network
as the only protection and keep it to the machines that hold the credentials.

### Snapshot admission ignores proxies

When a participant files a snapshot of an `http(s)` artifact, the hub verifies it with its own `HEAD`
request and **deliberately ignores `HTTP_PROXY`/`HTTPS_PROXY`** (an environment proxy would route the
request through a host the operator did not choose and defeat the internal-address check). A hub that can
only reach the outside through a proxy therefore cannot admit `http(s)` snapshots; they are refused or
stay unresolved. Either let the hub reach the artifact host directly, or name the host in
`ASOP_SNAPSHOT_HTTP_HOSTS` (the operator's allowlist for hosts on a private network). Put any such setting
in that hub's `hub.env` only.

## Connect an execution runtime or agent to exactly one hub per project

A runtime or agent is configured with one hub's URL, one actor and that actor's secret, in the
**project's own** environment, never in a global shell profile:

```sh
# in the project's environment (its own env file, direnv, or MCP server env block)
AGENTCO_REGISTRY_URL=http://127.0.0.1:8801     # or https://client-a.hub.example
AGENTCO_ACTOR=<actor>
AGENTCO_SECRET=<that actor's credential on client-a>
```

One project, one hub, one credential. If a repository serves two engagements, give each its own
checkout (or its own env file) so a command run in one cannot carry the other's credential. A credential
from `client-a` is refused by `personal`: if a tool ever works against two hubs with one secret, one of
them is misconfigured.

## Where secrets live, and what must never be shared

| Secret | Lives in | Never |
|---|---|---|
| actor credentials | that hub's `keys.json` (0600), and with the person or runtime that uses it | in another hub's table, in git, in chat |
| Postgres password (DSN) | that hub's `hub.env` (0600) | reused by another hub |
| a person's signing key | that person's partition state directory | on a hub, or reused across hubs |
| backups | a location as private as the hub, labelled with the hub | in the same folder as another hub's |

Never share between hubs: a port, a database or schema, a `keys.json` or any secret in it, a data or
log directory, a `hub.env`, a person's signing key, or a database file copied from another hub (see
below). A shared log directory is also a data leak: logs name actors and items.

## Backup and restore

```sh
hubctl backup client-a /path/to/backups/client-a-2026-01-01
```

For SQLite this uses the online backup API (consistent while the hub is writing, then an integrity
check); for Postgres it runs `pg_dump` with the password passed through the environment, not argv. The
destination is created 0700 and must not be inside any hub's directory. It contains the hub's credentials:
store it like the hub itself.

Restore **in place** (the hub is broken, or moved to new hardware): stop the service, replace
`<root>/<name>/data/registry.sqlite3` with the backup's file (mode 0600), put back `keys.json`, start,
run `hubctl status` and `hubctl check-isolation`. The generated hub id is stored in the database, so an
in-place restore keeps it, and a person's pinned id stays valid.

Restoring a backup **as a second hub** must not reuse the first hub's identity. A file-level copy carries
the `hub_uid` row, so both hubs would claim the same id and `check-isolation` fails with "carry the same
generated hub id". Remove the row from the copy before its first start (`sqlite3 <copy> "DELETE FROM meta
WHERE key = 'hub_uid'"`); the hub generates a fresh id, and anything bound to the old id (pins, enrolled
human keys) must be redone for the new hub. Also issue the second hub new credentials: a restored `keys.json`
shares secrets with the original, which `check-isolation` reports. Prefer
[`tools/backup_registry.py`](../../tools/backup_registry.py) (export and restore over HTTP into a freshly
created hub) when you want the procedures and their lineage without the identity.

## Upgrading

Upgrade one hub at a time. Take a `hubctl backup`, update the package in the interpreter the service
runs (the service records its interpreter path; re-run `hubctl service <name> --install` if you moved
it), restart that one service, then `hubctl status <name>` and `hubctl check-isolation`. Read the
upgrade notes first ([`../upgrading.md`](../upgrading.md) when present in your release). Keep hubs on the
same version unless you have a reason; each is independent, so a failed upgrade of `client-a` leaves
`personal` untouched.

## Removing a hub

```sh
hubctl service client-a --uninstall
hubctl backup client-a /path/to/archive/client-a-final    # only if you must keep anything
```

Then delete `<root>/client-a/` (database, keys, logs) and revoke the person-side material: remove the
hub's entry from each partition configuration (below) and delete that company's signing key and state
directory. Run `hubctl check-isolation` for the remaining hubs, and rotate nothing on them: they never
shared anything with the removed one.

## Person answering gates for several companies

One person can answer human gates for several company hubs from one machine. The rule is the same as
for the hubs: **one key, one transport credential, one state directory per company**, so nothing
signed or pinned for one company can be replayed at another. The full loop, with the second-channel
fingerprint and hub-id checks, is in
[`first-signed-answer-loop.md`](first-signed-answer-loop.md); this section only shows the per-company
configuration. For each company the operator runs `agentco human-key enrol` on that company's hub and
hands over that company's transport secret and hub id.

```sh
# company client-a
agentco partition keygen <state-dir>/client-a            # signing-key.pem (0600) + signing-key.pub.pem
export AGENTCO_PARTITION_LABEL=client-a
export AGENTCO_PARTITION_URL=https://client-a.hub.example
export AGENTCO_PARTITION_ACTOR=relay-<actor>
export AGENTCO_PARTITION_SECRET=<client-a transport secret>
export AGENTCO_PARTITION_SIGNER=<actor>
export AGENTCO_PARTITION_KEY=<state-dir>/client-a/signing-key.pem
export AGENTCO_PARTITION_STATE=<state-dir>/client-a/state
agentco partition pin <hub id the operator read out for client-a>

# company client-b: a different key, secret and state directory
agentco partition keygen <state-dir>/client-b
export AGENTCO_PARTITION_LABEL=client-b
export AGENTCO_PARTITION_URL=https://client-b.hub.example
export AGENTCO_PARTITION_ACTOR=relay-<actor>
export AGENTCO_PARTITION_SECRET=<client-b transport secret>
export AGENTCO_PARTITION_SIGNER=<actor>
export AGENTCO_PARTITION_KEY=<state-dir>/client-b/signing-key.pem
export AGENTCO_PARTITION_STATE=<state-dir>/client-b/state
agentco partition pin <hub id the operator read out for client-b>
```

Keep each block in its own env file (`<state-dir>/client-a/partition.env`, mode 0600) and load exactly one
per command, for example `env -i PATH="$PATH" sh -c '. <state-dir>/client-a/partition.env && agentco
partition answer ...'`, so a variable from one company can never reach a command meant for another. The
pin makes a mismatch loud: an answer addressed to the wrong hub is refused `hub_id_changed`, and a hub
that was never pinned is refused `hub_not_pinned`.
