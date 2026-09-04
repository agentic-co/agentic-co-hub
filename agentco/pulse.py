"""The pulse — the plane checks itself and everything wired to it, on a schedule.

The runtime this project grew out of had a heartbeat: the orchestrator wrote a
timestamp when a cycle completed, and the AGE of that file was the failure
signal — a crashed or wedged cycle never updated it. This plane runs no cycles,
so a heartbeat of that shape would prove nothing. What it has instead is state
that goes stale *silently*: a lease past expiry nobody has swept, a gate whose
park clock ran out with nobody looking, an actor that stopped publishing three
weeks ago and whose absence looks exactly like an actor with nothing to do.

Every check in here already existed as a function. `reap_expired_leases` had no
caller at all; the park-clock and quarantine sweeps ran only when a person typed
`agentco verifiers --sweep`. The pulse is the thing that runs them on a schedule
and says, in one place, what it found. Three rules, in the order they cost to get
wrong:

1. **Recovery never depends on the pulse.** A claim on an item whose lease has
   lapsed succeeds whether or not the reaper ran (`work.py`); a gate past its
   clock resolves the next time ANY sweep runs. The pulse makes those transitions
   visible and timely. It is never the only thing standing between the queue
   and a stall — a queue whose recovery depends on a cron job that quietly
   stopped is a queue that stalls with no error anywhere.

2. **The exit code is the worst consequence class, never a count.** Two silent
   actors and one silent actor are the same exit code, because a scheduler
   alerts on non-zero and a human reads the report. Counting would make "how
   many things are wrong" the alarm and "what is wrong" the afterthought.
   `fatal` means the plane itself cannot be trusted (unreadable store, a file at
   a schema this build does not know, an HTTP surface configured to refuse
   everyone); `attention` means something a person should act on; `ok` is
   everything else, housekeeping included.

3. **Dry-run by default.** Bare `agentco pulse` observes, reports, and changes
   nothing — the same posture as `digest` and `inject`. `--apply` runs the
   sweeps and records the run as a `PulseObserved` event, and that event is the
   pulse's own heartbeat: the next run, and the session hook, judge the gap
   against the cadence the run declared (`--every`). A pulse that stops is a
   finding too. That is the silent-schedule audit, applied first to the auditor.

What "connected to it" means here. There is no federation yet — the things wired
to a registry are the ACTORS publishing into it. The plane already sees every
one of them: each event carries its actor, each HTTP call is recorded with its
actor, each lease names its holder. That traffic is a harness's heartbeat, and
this module reads it rather than asking harnesses for a second one (a
dedicated heartbeat verb would spend the thirteenth MCP tool on information the
plane already has). What the traffic lacks is EXPECTATION: without a declared
cadence, an idle actor and a dead one are indistinguishable. `AGENTCO_CADENCE`
declares it per actor (`"alice=1d,ci-worker=2h"`); an actor with no declaration
is reported with `expectedEverySeconds: null` and never raises a finding —
unreported is `null`, not a guess.

One honest limit: over stdio (the MCP surface) nothing records a call, so an
actor whose only activity is empty `work_pull`s leaves no trace there. Over
HTTP every call counts, empty pulls included.
"""

from __future__ import annotations

import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

from agentco import events, migrations, verifiers
from agentco.work import WorkStatus, executors_of, pin_of

EVENT_KIND = "PulseObserved"

#: The actor the HTTP app records for a request that never authenticated.
UNAUTHENTICATED_ACTOR = "-"

CADENCE_ENV_VAR = "AGENTCO_CADENCE"
EVERY_ENV_VAR = "AGENTCO_PULSE_EVERY"
KEYS_ENV_VAR = "AGENTCO_REGISTRY_KEYS"

#: Consequence classes, worst last. The exit code is the index.
CLASSES = ("ok", "attention", "fatal")
EXIT_BY_CLASS = {name: code for code, name in enumerate(CLASSES)}

#: A pulse is "overdue" when the gap since the last recorded run exceeds this
#: many declared intervals. Two, not one: a scheduler that runs slightly long
#: is not an outage, and an audit that pages on jitter is an audit people mute.
OVERDUE_FACTOR = 2

#: How long a step bead may sit ready, bound to an actor that never pulls it,
#: before the pulse says so — used only for an actor with no declared cadence.
#: A day, because the failure this catches is a run bound to a label nobody
#: runs as, and nobody notices that in an hour; a shorter default would page
#: on every overnight queue. An actor WITH a cadence is judged against its own.
DEFAULT_STRANDED_SECONDS = 86400

_DURATION = re.compile(r"^\s*(\d+)\s*([smhd]?)\s*$", re.IGNORECASE)
_UNIT_SECONDS = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}


# --------------------------------------------------------------------------- #
# Declarations
# --------------------------------------------------------------------------- #


def parse_duration(text: str) -> int:
    """`"90"`, `"90s"`, `"15m"`, `"2h"`, `"1d"` → seconds. Anything else is refused.

    Refused rather than defaulted: a cadence that silently became "never"
    because of a typo is a silent actor nobody will ever be told about.
    """
    match = _DURATION.match(text or "")
    if not match:
        raise ValueError(
            f"{text!r} is not a duration — use an integer with an optional "
            f"s/m/h/d suffix, e.g. 900, 15m, 2h, 1d"
        )
    value, unit = match.groups()
    seconds = int(value) * _UNIT_SECONDS[unit.lower()]
    if seconds <= 0:
        raise ValueError(f"{text!r} is not a positive duration")
    return seconds


def cadences_from_env(value: Optional[str] = None) -> dict[str, int]:
    """`AGENTCO_CADENCE="alice=1d, ci-worker=2h"` → `{"alice": 86400, "ci-worker": 7200}`.

    Same shape as `AGENTCO_VERIFIERS` / `AGENTCO_HUMANS` (`policy.py`): a
    declaration in the environment, exactly as trustworthy as the environment
    file. A malformed entry raises, and the caller turns that into a `fatal`
    finding — a mis-declared expectation is worse than none, because it looks
    like one.
    """
    raw = value if value is not None else os.environ.get(CADENCE_ENV_VAR)
    out: dict[str, int] = {}
    for entry in (raw or "").split(","):
        entry = entry.strip()
        if not entry:
            continue
        if "=" not in entry:
            raise ValueError(
                f"{entry!r} in {CADENCE_ENV_VAR} has no '=' — the form is actor=duration"
            )
        actor, duration = (part.strip() for part in entry.split("=", 1))
        if not actor:
            raise ValueError(f"{entry!r} in {CADENCE_ENV_VAR} names no actor")
        out[actor] = parse_duration(duration)
    return out


def every_from_env(value: Optional[str] = None) -> Optional[int]:
    raw = value if value is not None else os.environ.get(EVERY_ENV_VAR)
    return parse_duration(raw) if raw and raw.strip() else None


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _parse(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def human_duration(seconds: Optional[float]) -> str:
    if seconds is None:
        return "never"
    total = int(seconds)
    if total < 60:
        return f"{total}s"
    parts = []
    for label, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if total >= size:
            parts.append(f"{total // size}{label}")
            total %= size
        if len(parts) == 2:
            break
    return " ".join(parts)


def _finding(check: str, klass: str, detail: str, remediation: Optional[str] = None) -> dict:
    assert klass in CLASSES
    return {"check": check, "class": klass, "detail": detail, "remediation": remediation}


def worst(findings: Iterable[dict]) -> str:
    rank = 0
    for f in findings:
        rank = max(rank, EXIT_BY_CLASS[f["class"]])
    return CLASSES[rank]


# --------------------------------------------------------------------------- #
# The checks
# --------------------------------------------------------------------------- #


def check_plane(conn: sqlite3.Connection, *, now: datetime) -> tuple[dict, list[dict]]:
    """The registry file itself: schema known, integrity intact, writable, feed alive.

    "Pending migrations" is not a state this can observe — `db.connect` applies
    them on open, so by the time there is a connection the file is current or
    the open failed. What CAN be observed is the opposite: a file carrying
    versions this build has never heard of, which means a newer build wrote it
    and this one is about to misread rows it does not understand.
    """
    findings: list[dict] = []
    known = {m.version for m in migrations.MIGRATIONS}
    applied = migrations.applied_versions(conn)
    unknown = sorted(applied - known)
    if unknown:
        findings.append(_finding(
            "plane.schema", "fatal",
            f"the registry file carries schema version(s) {unknown} this build does not know "
            f"(this build knows up to {max(known)})",
            "Upgrade this agentco to at least the version that wrote the file; do not run "
            "an older build against it.",
        ))

    integrity = conn.execute("PRAGMA quick_check(1)").fetchone()[0]
    if integrity != "ok":
        findings.append(_finding(
            "plane.integrity", "fatal", f"SQLite quick_check reports: {integrity}",
            "Stop writers, take a copy of the file, and run `sqlite3 <file> '.recover'`.",
        ))

    writable = True
    try:
        if not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("ROLLBACK")
    except sqlite3.OperationalError as exc:
        writable = False
        findings.append(_finding(
            "plane.writable", "fatal", f"could not take a write lock: {exc}",
            "Check the file's permissions and whether another process holds it open for "
            "longer than the 30s busy timeout.",
        ))

    row = conn.execute(
        "SELECT COUNT(*) AS n, MAX(occurred_at) AS last FROM events WHERE kind != ?",
        (EVENT_KIND,),
    ).fetchone()
    last = _parse(row["last"])
    facts = {
        "schemaVersion": max(applied) if applied else None,
        "integrity": integrity,
        "writable": writable,
        "events": row["n"],
        "lastEventAt": row["last"],
        "lastEventAgeSeconds": int((now - last).total_seconds()) if last else None,
    }
    return facts, findings


def check_keys(keys_env: Optional[str]) -> tuple[dict, list[dict]]:
    """Is the HTTP surface configured to let anyone in?

    Unset is not a finding: a stdio-only deployment has no key file and the
    HTTP surface, if anyone starts it, refuses everyone — which is the safe
    failure. SET-but-empty is `fatal`, because the operator believes actors can
    publish over HTTP and every one of them is being refused, and a refused
    first publish is the adoption failure this project names as lethal.
    """
    from agentco import auth

    if not keys_env:
        return {"configured": None, "actors": 0}, []
    try:
        keys = auth.load_keys(keys_env)
    except auth.AmbiguousIdentityError as exc:
        return {"configured": True, "actors": 0}, [_finding(
            "keys.ambiguous", "fatal", str(exc),
            "Fix the key file; until then every HTTP request is refused.",
        )]
    if not keys:
        return {"configured": True, "actors": 0}, [_finding(
            "keys.empty", "fatal",
            f"{KEYS_ENV_VAR} points at {keys_env!r} but no keys loaded from it — the HTTP "
            f"surface is refusing every request",
            "Check the path exists, is valid JSON of {actor: secret}, and is readable by "
            "the process running the registry.",
        )]
    return {"configured": True, "actors": len(keys)}, []


def check_stores(queue, library=None) -> tuple[dict, list[dict]]:
    """Both stores open and parse. A store that does not is `fatal` — every
    surface reading it presents an EMPTY queue, not an error."""
    findings: list[dict] = []
    facts: dict[str, Any] = {"workItems": None, "byStatus": {}, "activeSops": None}
    try:
        items = queue.list()
        by_status: dict[str, int] = {}
        for item in items:
            by_status[item.status.value] = by_status.get(item.status.value, 0) + 1
        facts["workItems"] = len(items)
        facts["byStatus"] = by_status
    except Exception as exc:  # noqa: BLE001 - the whole point is to name the failure
        findings.append(_finding(
            "stores.work", "fatal", f"work store unreadable ({type(exc).__name__}: {exc})",
            "Every surface reading this store sees an empty queue rather than an error. "
            "Restore the file or fix its path before trusting any 'nothing to do'.",
        ))
    if library is not None:
        try:
            facts["activeSops"] = len(library.list_active())
        except Exception as exc:  # noqa: BLE001
            findings.append(_finding(
                "stores.sop", "fatal", f"SOP library unreadable ({type(exc).__name__}: {exc})",
                "Instances pinned to a procedure cannot be created until this reads again.",
            ))
    return facts, findings


def housekeeping(queue, conn: sqlite3.Connection, *, now: datetime, apply: bool) -> tuple[dict, list[dict]]:
    """Run (or preview) the sweeps that already existed and nobody was running.

    None of these is `attention` on its own. An expired lease returning to
    ready and a gate resolving by its declared default are the system working;
    what earns a finding is the state the sweeps cannot fix — a gate abandoned
    past quarantine, or a queue approving its own work on the clock with no
    verdict behind any gate (`verifier_status`'s warning).
    """
    findings: list[dict] = []
    if apply:
        reaped = [i.id for i in queue.reap_expired_leases(now)]
    else:
        reaped = [
            i.id for i in queue.list(WorkStatus.IN_PROGRESS)
            if i.leased_by and not i.lease_active_at(now)
        ]
    # Routing BEFORE the clocks. Found live, not in a test: a judged gate parked
    # by a report over HTTP emitted nothing on the feed until its clock ran out,
    # because `WorkParked` is the routing pass's event and nothing was running
    # that pass. A parked gate nobody can see is the abandonment the whole L3
    # design exists to prevent, and it was one unscheduled command away.
    routing = verifiers.route_open_gates(queue, conn=conn, dry_run=not apply)
    park = verifiers.sweep_park_clocks(queue, now=now, conn=conn, dry_run=not apply)
    quarantine = verifiers.sweep_quarantine(queue, now=now, dry_run=not apply)
    # Runs whose every step is done but whose container never heard about it.
    # The report path closes these as the last step lands (§5.5); this is the
    # repair for the ones that finished before that shipped, and for the crash
    # window where a child's write commits and the process dies before the
    # container's does. Not new logic — the same function, applied late.
    stranded = queue.close_finished_runs(dry_run=not apply)
    stuck = verifiers.quarantine_digest(queue, now=now)
    status = verifiers.verifier_status(queue, now=now)

    if stuck["count"]:
        oldest = stuck["stuckGates"][0] if stuck.get("stuckGates") else None
        detail = f"{stuck['count']} gate(s) abandoned past quarantine"
        if oldest and oldest.get("unansweredSeconds") is not None:
            detail += f", the oldest unanswered for {human_duration(oldest['unansweredSeconds'])}"
        findings.append(_finding(
            "housekeeping.quarantine", "attention", detail,
            "Someone named as escalation has to answer these; the queue will not close them. "
            "`agentco verifiers` lists them oldest first.",
        ))
    if status.get("warning"):
        findings.append(_finding(
            "housekeeping.verifiers", "attention", status["warning"],
            "Declare a verifier (AGENTCO_VERIFIERS) or stop routing gates with on_timeout: pass.",
        ))

    facts = {
        "applied": apply,
        "expiredLeases": reaped,
        "routing": {
            "created": len(routing.get("created", [])),
            "retired": len(routing.get("retired", [])),
        },
        "parkClocks": {
            "resolved": len(park.get("resolved", [])),
            "escalated": len(park.get("escalated", [])),
        },
        "quarantine": {"quarantined": len(quarantine.get("quarantined", []))},
        "strandedRuns": {("closed" if apply else "wouldClose"): len(stranded)},
        "stuck": stuck["count"],
        "verifier": {"configured": status.get("configured"), "warning": status.get("warning")},
    }
    return facts, findings


def last_seen(conn: sqlite3.Connection, queue) -> dict[str, datetime]:
    """actor → the most recent moment the plane saw them do anything.

    Three sources, because no one of them sees every transport: the change feed
    (every publish, on every transport), the calls table (every HTTP request,
    refusals and empty pulls included), and the work store (who holds or has
    executed an item). The plane's own reserved actor is excluded — it is not a
    participant.
    """
    seen: dict[str, datetime] = {}

    def note(actor: Optional[str], stamp: Optional[str]) -> None:
        # `-` is what the HTTP app records as the actor of a request that
        # never authenticated (`app.py`). Found live: one unauthenticated curl
        # against /events made "-" a participant. Nobody is called "-".
        if not actor or actor in (events.PLANE_ACTOR, UNAUTHENTICATED_ACTOR):
            return
        when = _parse(stamp)
        if when and (actor not in seen or when > seen[actor]):
            seen[actor] = when

    for row in conn.execute(
        "SELECT actor, MAX(occurred_at) AS last FROM events WHERE kind != ? GROUP BY actor",
        (EVENT_KIND,),
    ):
        note(row["actor"], row["last"])
    for row in conn.execute("SELECT actor, MAX(at) AS last FROM calls GROUP BY actor"):
        note(row["actor"], row["last"])
    try:
        items = queue.list()
    except Exception:  # noqa: BLE001 - already reported by check_stores
        items = []
    for item in items:
        note(item.leased_by, item.updated_at)
        for executor in executors_of(item):
            note(executor, item.updated_at)
    return seen


def check_participants(
    conn: sqlite3.Connection, queue, *, now: datetime, cadences: dict[str, int]
) -> tuple[list[dict], list[dict]]:
    """Who is wired to this plane, and who has gone quiet past what they declared.

    A declared actor never seen is `attention` too — the declaration says
    something should be publishing, and nothing is. An undeclared actor is
    listed with `expectedEverySeconds: null` and raises nothing: absence of an
    expectation is not evidence of a problem, and inventing a default here
    would make every idle laptop an alarm.
    """
    seen = last_seen(conn, queue)
    rows: list[dict] = []
    findings: list[dict] = []
    for actor in sorted(set(seen) | set(cadences)):
        when = seen.get(actor)
        expected = cadences.get(actor)
        silent = int((now - when).total_seconds()) if when else None
        if expected is None:
            state = "undeclared"
        elif when is None:
            state = "never-seen"
        elif silent > expected:
            state = "silent"
        else:
            state = "active"
        rows.append({
            "actor": actor,
            "lastSeenAt": _iso(when) if when else None,
            "silentSeconds": silent,
            "expectedEverySeconds": expected,
            "state": state,
        })
        if state == "silent":
            findings.append(_finding(
                "participants.silent", "attention",
                f"{actor} last seen {human_duration(silent)} ago, declared every "
                f"{human_duration(expected)}",
                f"Check whether {actor}'s harness, scheduler, or credentials died — or drop "
                f"it from {CADENCE_ENV_VAR} if it is meant to be idle.",
            ))
        elif state == "never-seen":
            findings.append(_finding(
                "participants.never-seen", "attention",
                f"{actor} is declared every {human_duration(expected)} and has never been seen",
                f"Confirm {actor} is pointed at THIS registry, or drop it from {CADENCE_ENV_VAR}.",
            ))
    return rows, findings


def check_stranded_steps(
    queue, *, now: datetime, cadences: dict[str, int]
) -> tuple[list[dict], list[dict]]:
    """Step beads sitting ready, bound to an actor that has never pulled them.

    A run's bindings name actor labels, and the runtime pulls as its own
    configured actor. Get the label wrong — a typo, a renamed worker, a
    binding written for a machine that was decommissioned — and the step is
    filed, ready, correct in every other respect, and claimed by nobody. It
    is `pending` forever, which looks exactly like a queue with nothing in it.

    Filing already refuses a binding the registry has no key for; this is the
    other half, and it is the half a key file cannot answer: an actor CAN
    authenticate and still never run. Only the passage of time says so.

    `attention`, never fatal — a step waiting on a worker that is starting up,
    or on a person who has not read their queue yet, is not an outage. What
    makes it a finding is that nothing else in the plane will ever mention it.

    "Ready since" is derived, because the queue records no such moment: it is
    the later of the bead's own creation and the last of its blockers closing.
    A bead with an unmet blocker is not ready and is not counted.
    """
    items = queue.list()
    by_id = {i.id: i for i in items}
    rows: list[dict] = []
    findings: list[dict] = []

    for item in items:
        actor = item.assigned_agent
        ref = pin_of(item)
        if not actor or "step" not in ref or item.status is not WorkStatus.PENDING:
            continue
        blockers = [by_id.get(b) for b in (item.blocked_by or [])]
        if any(b is None or b.status is not WorkStatus.DONE for b in blockers):
            continue  # not ready yet; waiting on a step is not being stranded
        if item.leased_by or (item.metadata or {}).get("claims"):
            continue  # somebody has pulled it; this is about the ones nobody has
        ready_since = max(
            [_parse_at(item.created_at)]
            + [_parse_at(b.updated_at) for b in blockers if b is not None]
        )
        if ready_since is None:
            continue
        waited = int((now - ready_since).total_seconds())
        expected = cadences.get(actor, DEFAULT_STRANDED_SECONDS)
        rows.append({
            "itemId": item.id,
            "title": item.title,
            "actor": actor,
            "asopId": ref.get("asop_id"),
            "step": ref.get("step"),
            "readySince": _iso(ready_since),
            "waitingSeconds": waited,
            "expectedEverySeconds": cadences.get(actor),
        })
        if waited <= expected:
            continue
        declared = (f"declared every {human_duration(expected)}" if actor in cadences
                    else f"no declared cadence, so the {human_duration(expected)} default applies")
        findings.append(_finding(
            "housekeeping.stranded-steps", "attention",
            f"stranded step: {item.id} bound to {actor!r}, ready since "
            f"{_iso(ready_since)} ({human_duration(waited)}), never pulled — {declared}",
            f"Check that something actually pulls as {actor!r}: the binding is a LABEL, "
            f"and a run bound to a label nobody runs as is filed, ready, and claimed by "
            f"nobody. Re-file the run with a binding that matches the worker's configured "
            f"actor, or start the worker.",
        ))
    return rows, findings


def _parse_at(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def last_observed(conn: sqlite3.Connection) -> Optional[dict]:
    """The most recent recorded pulse, or None. Read by the session hook."""
    row = conn.execute(
        "SELECT occurred_at, payload FROM events WHERE kind = ? ORDER BY seq DESC LIMIT 1",
        (EVENT_KIND,),
    ).fetchone()
    if row is None:
        return None
    import json

    payload = json.loads(row["payload"])
    return {"at": row["occurred_at"], **payload}


def self_audit(conn: sqlite3.Connection, *, now: datetime, every: Optional[int]) -> tuple[dict, list[dict]]:
    """The silent-schedule audit, applied to the auditor.

    Without a declared interval there is nothing to judge against, and the
    section says so rather than guessing. With one, a gap over `OVERDUE_FACTOR`
    intervals is `attention`: the thing that was supposed to notice silence went
    silent, which is the one failure no other check here can report.
    """
    previous = last_observed(conn)
    gap = None
    if previous:
        when = _parse(previous["at"])
        gap = int((now - when).total_seconds()) if when else None
    facts = {
        "every": every,
        "lastPulseAt": previous["at"] if previous else None,
        "gapSeconds": gap,
        "state": "undeclared" if every is None else ("first-run" if previous is None else "on-time"),
    }
    findings: list[dict] = []
    if every is not None and gap is not None and gap > OVERDUE_FACTOR * every:
        facts["state"] = "overdue"
        findings.append(_finding(
            "self.silent", "attention",
            f"the pulse itself last ran {human_duration(gap)} ago against a declared "
            f"interval of {human_duration(every)}",
            "The scheduler running `agentco pulse --apply` stopped or is failing before it "
            "records; check its logs, not the registry's.",
        ))
    return facts, findings


# --------------------------------------------------------------------------- #
# The pass
# --------------------------------------------------------------------------- #


def run(
    conn: sqlite3.Connection,
    queue,
    *,
    library=None,
    now: Optional[datetime] = None,
    apply: bool = False,
    every: Optional[int] = None,
    cadences: Optional[dict[str, int]] = None,
    keys_env: Optional[str] = "",
) -> dict:
    """One pulse. Returns the report; `apply` also runs the sweeps and records it.

    `keys_env` defaults to the sentinel `""` meaning "read the environment";
    pass `None` to assert the variable is unset (tests, and any caller that has
    already resolved it).
    """
    at = now or _now()
    if keys_env == "":
        keys_env = os.environ.get(KEYS_ENV_VAR)
    if cadences is None:
        try:
            cadences = cadences_from_env()
            cadence_finding = None
        except ValueError as exc:
            cadences = {}
            cadence_finding = _finding(
                "participants.declaration", "fatal", str(exc),
                f"Fix {CADENCE_ENV_VAR}; until then no actor's silence can be judged.",
            )
    else:
        cadence_finding = None

    findings: list[dict] = []
    plane, f = check_plane(conn, now=at)
    findings += f
    keys, f = check_keys(keys_env)
    findings += f
    stores, f = check_stores(queue, library)
    findings += f
    house, f = housekeeping(queue, conn, now=at, apply=apply)
    findings += f
    if cadence_finding:
        findings.append(cadence_finding)
    participants, f = check_participants(conn, queue, now=at, cadences=cadences)
    findings += f
    stranded, f = check_stranded_steps(queue, now=at, cadences=cadences)
    findings += f
    own, f = self_audit(conn, now=at, every=every)
    findings += f

    klass = worst(findings)
    report = {
        "generatedAt": _iso(at),
        "applied": apply,
        "class": klass,
        "exitCode": EXIT_BY_CLASS[klass],
        "findings": findings,
        "plane": plane,
        "keys": keys,
        "stores": stores,
        "housekeeping": house,
        "participants": participants,
        "strandedSteps": stranded,
        "self": own,
    }

    if apply:
        # The heartbeat row. Recorded AFTER the checks so a pulse that crashes
        # mid-pass leaves no row, and the gap is the signal — the one property
        # the old heartbeat file had that is worth keeping.
        by_class = {name: 0 for name in CLASSES}
        for finding in findings:
            by_class[finding["class"]] += 1
        recorded = events.append(
            conn,
            kind=EVENT_KIND,
            actor=events.PLANE_ACTOR,
            occurred_at=report["generatedAt"],
            payload={
                "class": klass,
                "findings": by_class,
                "every": every,
                "silentParticipants": sum(1 for p in participants if p["state"] == "silent"),
                "strandedSteps": sum(
                    1 for r in stranded
                    if r["waitingSeconds"] > (r["expectedEverySeconds"] or DEFAULT_STRANDED_SECONDS)
                ),
                "housekeeping": {
                    "expiredLeases": len(house["expiredLeases"]),
                    "routed": house["routing"]["created"],
                    "parkClocksResolved": house["parkClocks"]["resolved"],
                    "parkClocksEscalated": house["parkClocks"]["escalated"],
                    "quarantined": house["quarantine"]["quarantined"],
                    "stuck": house["stuck"],
                },
            },
        )
        report["recorded"] = {"seq": recorded["seq"], "cursor": recorded["cursor"]}
    return report


def exit_code(report: dict) -> int:
    return EXIT_BY_CLASS[report["class"]]


def render_text(report: dict) -> str:
    plane, keys, stores = report["plane"], report["keys"], report["stores"]
    house, own = report["housekeeping"], report["self"]
    n = len(report["findings"])
    head = f"pulse — {report['class'].upper()} ({n} finding{'s' if n != 1 else ''}) at {report['generatedAt']}"
    if not report["applied"]:
        head += "  [dry run — nothing swept, nothing recorded; add --apply]"
    lines = [head]

    age = plane["lastEventAgeSeconds"]
    lines.append(
        f"plane: schema v{plane['schemaVersion']}, integrity {plane['integrity']}, "
        f"{'writable' if plane['writable'] else 'NOT WRITABLE'}, {plane['events']} event(s)"
        + (f", last {human_duration(age)} ago" if age is not None else ", none yet")
    )
    if keys["configured"] is None:
        lines.append("keys: none configured (HTTP refuses everyone; stdio unaffected)")
    else:
        lines.append(f"keys: {keys['actors']} actor(s)")
    by_status = ", ".join(f"{k} {v}" for k, v in sorted(stores["byStatus"].items()))
    lines.append(
        f"stores: {stores['workItems']} work item(s)"
        + (f" ({by_status})" if by_status else "")
        + (f", {stores['activeSops']} active SOP(s)" if stores["activeSops"] is not None else "")
    )
    verb = "returned to ready" if report["applied"] else "would return to ready"
    lines.append(
        f"housekeeping: {len(house['expiredLeases'])} expired lease(s) {verb}; "
        f"gates routed {house['routing']['created']}; "
        f"park clocks {house['parkClocks']['resolved']} resolved, "
        f"{house['parkClocks']['escalated']} escalated; "
        f"quarantine {house['quarantine']['quarantined']} new, {house['stuck']} stuck"
    )
    parts = report["participants"]
    silent = [p for p in parts if p["state"] in ("silent", "never-seen")]
    undeclared = sum(1 for p in parts if p["state"] == "undeclared")
    lines.append(
        f"participants: {len(parts)} known, {len(silent)} past declared cadence, "
        f"{undeclared} with no declared cadence"
    )
    for p in silent:
        seen = f"last seen {human_duration(p['silentSeconds'])} ago" if p["silentSeconds"] is not None else "never seen"
        lines.append(f"  - {p['actor']}: {seen}, declared every {human_duration(p['expectedEverySeconds'])}")
    if own["every"] is None:
        lines.append("self: no interval declared (--every / AGENTCO_PULSE_EVERY), so the pulse's own silence is not judged")
    elif own["lastPulseAt"] is None:
        lines.append(
            f"self: {'this is the first recorded run' if report['applied'] else 'no pulse recorded yet'} "
            f"(every {human_duration(own['every'])})"
        )
    else:
        lines.append(
            f"self: last pulse {human_duration(own['gapSeconds'])} ago, every "
            f"{human_duration(own['every'])} — {own['state']}"
        )
    if report["findings"]:
        lines.append("findings:")
        for f in sorted(report["findings"], key=lambda x: -EXIT_BY_CLASS[x["class"]]):
            lines.append(f"  ! [{f['class']}] {f['check']}: {f['detail']}")
            if f.get("remediation"):
                lines.append(f"      → {f['remediation']}")
    return "\n".join(lines)


def render_session_line(observed: Optional[dict], *, now: Optional[datetime] = None) -> Optional[str]:
    """One line for the session hook, or None when no pulse has ever been recorded.

    Nothing is rendered for a registry that has never run one: an L1 publisher
    with no scheduler should not be told about a pass that is not theirs to run.
    Once one exists, its age is judged against the interval IT declared, so the
    hook needs no environment of its own to say "overdue".
    """
    if not observed:
        return None
    at = _parse(observed.get("at"))
    if at is None:
        return None
    gap = int(((now or _now()) - at).total_seconds())
    total = sum((observed.get("findings") or {}).values())
    line = f"Pulse: last ran {human_duration(gap)} ago — {observed.get('class', '?')}, {total} finding(s)"
    every = observed.get("every")
    if every and gap > OVERDUE_FACTOR * every:
        line += f" ⚠ overdue (declared every {human_duration(every)})"
    return line
