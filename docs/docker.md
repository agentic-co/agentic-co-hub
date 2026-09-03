# Running the registry in Docker

One container, one SQLite file on a named volume, port published to loopback.

```bash
python3 -m agentco keygen you > keys.json && chmod 600 keys.json
docker compose up -d --build registry
docker compose ps          # healthy = an unauthenticated request is being refused
```

The service is then on `http://127.0.0.1:8787`, and `agentco/publish.py` reaches
it unchanged:

```python
reg = Registry("you", SECRET, "http://127.0.0.1:8787")
```

## The four decisions in these files

**The port is published to `127.0.0.1` only.** Authentication is a per-actor
shared secret over plain HTTP, so on a routable interface the secrets cross the
network in the clear. Exposing it is a deliberate act: put TLS in front of it
and change that line where a reviewer can see you did.

**The server binds `0.0.0.0` inside the container, which is not the same act.**
A container's own loopback reaches nothing, so binding it there would make the
service unreachable rather than private. The CLI's loopback default protects the
decision to *publish*, and that decision lives in `docker-compose.yml`.

**`git` is in the image, and your repos are not.** `snapshots.resolve_git`
shells out to `git rev-parse` to read a pointer's version token. Without git
installed, every `git:` snapshot is still recorded but permanently unresolvable
— it can never report divergence, which is the silent half of the feature. The
repos themselves have to be mounted in; the resolver reads a filesystem path,
and a path this container cannot see is a path it cannot resolve:

```bash
AGENTCO_REPOS=$HOME/code docker compose up -d registry
# then snapshot as the CONTAINER sees it:
reg.snapshot("git:/repos/web-platform#main", "baseline for the redesign")
```

Mounted read-only, on purpose. The resolver only ever runs `rev-parse`, and a
coordination layer with write access to your repositories is a different threat
model than the one this project argues for.

**Never more than one server process against one file.** `serve` takes no
`--workers` and must not grow one. Conflict detection reads the live leases and
writes the new lease in separate transactions, so two processes can each miss
the other's claim and both report zero conflicts — the exact blindness the
registry exists to remove.

## The cadence job

Divergence is delivered at a boundary the team chooses, so the digest is a
one-shot rather than a sidecar — a container that picked the schedule would take
the product decision away from the team:

```bash
docker compose run --rm digest                          # print, deliver nothing
docker compose run --rm digest digest --deliver         # at the boundary
AGENTCO_DIGEST_WEBHOOK=https://... \
  docker compose run --rm digest digest --deliver --post
```

Put the second line in cron or a systemd timer on the host. Note the known issue
first: one unreachable pointer currently aborts the whole digest run
([`known-issues.md`](known-issues.md)), so a scheduled job should alert on a
non-zero exit rather than assume "no output means nothing moved".

## The pulse

```bash
docker compose run --rm pulse                                 # dry run: look, change nothing
AGENTCO_CADENCE="alice=1d,ci-worker=2h" \
  docker compose run --rm pulse pulse --apply --every 15m     # the scheduled form
```

Same posture as the digest: the host owns the schedule. The exit code is the worst
consequence class — `0` ok, `1` attention, `2` the plane itself cannot be trusted —
so a timer should alert on non-zero and never on the count of findings. Details
and what each check means: [`pulse.md`](pulse.md).

## Postgres, as an opt-in profile

Everything above is the default: one SQLite file on `agentco-state`. Compose
also ships a `pg` profile — a `postgres` service that only exists when asked
for, so the default path (`docker compose up -d registry`, no profile flag)
is unaffected either way:

```bash
AGENTCO_PG_PASSWORD=$(openssl rand -hex 24) \
  docker compose --profile pg up -d postgres
AGENTCO_PG_PASSWORD=... AGENTCO_PG_DSN="postgresql://agentco:${AGENTCO_PG_PASSWORD}@postgres:5432/agentco" \
  docker compose --profile pg up -d --build registry
```

`AGENTCO_PG_DSN` is the one variable that matters: `registry` (and `digest`/
`pulse`, when run) forward it straight to `AGENTCO_DB` — see
[`docs/architecture.md`](architecture.md#storage) for what changes underneath
(nothing in the SQL; `agentco/pgadapter.py` is a connection adapter, not a
second dialect) and [`docs/roadmap.md`](roadmap.md#where-the-stores-live) for
the resolution table. Leave `AGENTCO_PG_DSN` unset and `registry` falls back
to the SQLite file on `agentco-state`, exactly as before this profile
existed.

`AGENTCO_PG_PASSWORD` has a checked-in placeholder default
(`change-me-before-using-the-pg-profile`) rather than a required-variable
error, because Compose interpolates every service's `environment:` block
while parsing the file regardless of which profile is active — a hard
failure there would break the DEFAULT, no-Postgres path too. Set it for
real before this profile touches anything but a throwaway local database.

Cutting an existing SQLite deployment over, rather than starting fresh:
stop `registry` first, copy `registry.sqlite3`/`work.jsonl`/`sops.jsonl`
off the `agentco-state` volume, run
[`tools/migrate_sqlite_to_pg.py`](../tools/migrate_sqlite_to_pg.py) against
the copies and the new database, then start `registry` with
`AGENTCO_PG_DSN` set. Never point the migration tool at the live files —
copy first.

## What is not containerised, and why

**The MCP surface.** `serve-mcp` speaks JSON-RPC on stdio and is launched *by*
the harness, one process per client — a long-running container is the wrong
shape for it, and putting `docker run` in someone's `.mcp.json` adds a startup
cost to every session for nothing. Point the harness at the checkout.

**`inject` and `hook install`.** Both edit files in someone's home directory or
repository. Reaching those from inside a container means mounting the very
things the tool is careful about writing to, and the commands are dry-run,
single-purpose, and stdlib-only — run them on the host.

## Healthcheck

`tools/docker/healthcheck.py` requests `/events` with no credentials and treats
**401 as healthy**. That is the only response that proves both that the app is
serving and that the key table loaded *closed*: a `200` would mean
`auth.load_keys` failed open and the registry is answering anonymously, so the
check exits non-zero on it rather than reporting it as health.

## State, backup, and upgrade

Everything mutable is on the `agentco-state` volume — `registry.sqlite3` (plus
its WAL sidecars), `work.jsonl`, `sops.jsonl`. The image holds no state.

```bash
docker run --rm -v agentco-state:/state -v "$PWD":/out alpine \
  tar czf /out/agentco-state.tgz -C /state .
```

Take that with the container **stopped**. SQLite in WAL mode keeps recent
commits in `-wal` until a checkpoint, and copying the three files while a writer
is live can capture a torn set.

There is no schema migration path yet: `db.SCHEMA_VERSION` is written into the
`meta` table on creation and never checked on open, so an older image opening a
newer file will not tell you. Until that exists, upgrading means keeping the
backup above.
