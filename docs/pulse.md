# The pulse

`agentco pulse` is the plane checking itself and everything wired to it, on a
schedule you choose. It is the heartbeat from the runtime this project grew out
of, inverted for a plane that runs no cycles.

## Why not a heartbeat

The old orchestrator wrote `heartbeat.json` when a cycle completed, and the age of
that file was the failure signal — a crashed cycle never updated it. That worked
because there was a cycle. This plane executes nothing; it holds state that other
harnesses write. What goes wrong here is not "the loop stopped" but "the state
went stale and nobody noticed":

- a lease past expiry that no reaper has swept — the item self-heals on the next
  claim, but nothing shows it as ready in the meantime, and `reap_expired_leases`
  had **no caller anywhere** in the package;
- a gate whose park clock ran out — the sweep that resolves it only ran when a
  person typed `agentco verifiers --sweep`;
- an actor that stopped publishing three weeks ago, which looks exactly like an
  actor with nothing to do.

So the pulse does not prove it ran. It reads the traffic the plane already sees,
runs the sweeps that already existed, and says in one place what it found.

## What one pulse does

| Section | Checks | Consequence |
|---|---|---|
| **plane** | the registry file carries no schema version this build does not know; SQLite `quick_check` is `ok`; a write lock can be taken; age of the last event | schema / integrity / lock failures are **fatal** |
| **keys** | if `AGENTCO_REGISTRY_KEYS` is set, keys actually loaded | set-but-empty is **fatal** (the HTTP surface is refusing everyone). Unset is not a finding — a stdio-only deployment has no key file |
| **stores** | the work store and SOP library open and parse, with counts by status | unreadable is **fatal** — every surface would show an *empty* queue, not an error |
| **housekeeping** | expired leases, park clocks, quarantine, stranded runs — previewed, or run under `--apply` | none of these is a finding on its own: that is the system working. Gates abandoned past quarantine, and a queue resolving gates on the clock with no verdict behind any (`verifier_status`'s warning), are **attention** |
| **participants** | every actor the plane has seen, with their last activity and, if declared, their expected cadence | declared and silent past cadence, or declared and never seen, is **attention**. Undeclared is reported with `expectedEverySeconds: null` and never raises |
| **self** | the gap since the last recorded pulse, against the interval that run declared | over twice the interval is **attention** — the thing that watches for silence went silent |

### Consequence classes, not counts

Exit code is the **worst class present**: `0` ok, `1` attention, `2` fatal. Two
silent actors and one silent actor exit the same way. A scheduler alerts on
non-zero; a human reads the report for *what*. Counting would make "how many
things are wrong" the alarm and "what is wrong" the afterthought.

### Dry-run by default

Bare `agentco pulse` observes and changes nothing — no bytes in the work store,
no rows in the feed. That is the same posture as `digest` and `inject`, and there
is a test that fails if it stops being true. `--apply`:

- runs `reap_expired_leases` (items back to `pending`, attempt advanced so the
  reaped holder's late report is fenced out),
- runs `sweep_park_clocks` and `sweep_quarantine`,
- closes run containers whose every step is done but which never heard about
  it — a REPAIR, not a second implementation: it calls the same
  `finished_containers` the report path cascades through, for the runs that
  finished before that shipped and for the crash window between a child's
  write and its container's,
- appends one `PulseObserved` event, attributed to the plane's reserved actor.

That event is the pulse's own heartbeat. It is written **after** the checks, so a
pass that crashes leaves no row and the gap is the signal — the one property of
the old heartbeat file worth keeping.

## Declaring expectations

The plane sees every actor: each event carries its actor, each HTTP call is
recorded with its actor (refusals and empty pulls included), each lease names its
holder. That traffic is a harness's heartbeat; the plane does not ask for a
second one, and a dedicated heartbeat verb would spend the thirteenth MCP tool on
information it already has.

What the traffic lacks is *expectation*. Declare it per actor:

```bash
export AGENTCO_CADENCE="alice=1d,ci-worker=2h,release-bot=30m"
export AGENTCO_PULSE_EVERY=15m        # or pass --every
```

Durations are an integer with an optional `s`/`m`/`h`/`d` suffix. A malformed
entry is a **fatal** finding, not a silent default: a cadence that quietly became
"never" is a silent actor nobody will be told about.

One honest limit: over stdio (the MCP surface) nothing records a call, so an
actor whose only activity is empty `work_pull`s leaves no trace there. Its
claims, snapshots, reports, and leases all count; over HTTP every call counts.

## Scheduling it

```bash
# every 15 minutes, from cron / systemd / launchd
python3 -m agentco pulse --apply --every 15m
```

Alert on a non-zero exit. `--json` gives the full report for anything that wants
to render it. The session hook (`agentco hook install`) shows one line about the
last recorded pulse — its age, class, and finding count, with an overdue flag
when the gap exceeds twice the interval that run declared — so a person opening
a session learns the pulse stopped without having to remember it exists. A
registry that has never recorded one shows nothing: an L1 publisher should not be
told about a pass that is not theirs to run.

## What it never does

- **Become load-bearing for recovery.** A claim on an item whose lease lapsed
  succeeds whether or not the reaper ran (`work.py`); a gate past its clock
  resolves the next time *any* sweep runs. A queue whose recovery depends on a
  cron job that quietly stopped is a queue that stalls with no error anywhere.
- **Execute work.** The plane stores claims and never runs the check; the pulse
  inherits that.
- **Guess.** An undeclared actor's expectation is `null`. A pulse with no
  declared interval reports its gap and does not judge it.
