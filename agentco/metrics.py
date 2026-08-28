"""Stage 1d — the the adoption gate instrument, as queries rather than a second build.

The roadmap lists measurement as its own workstream: "weekly
active publishers, time-to-first-event, per-verb latency, and one
privately-collected saved-time story." It is folded into 1b here because
every one of those numbers is a query over the `calls` and `events` tables
1b has to write anyway — and because building it afterwards means four weeks
elapse before the gate can be evaluated, and then the clock starts.

**the adoption gate is the only gate that decides whether the architecture gets built:**

    "≥2 identities other than the author's are weekly active publishers for
    four consecutive weeks. Not 'have made a call' — an earlier draft of this
    gate asked whether the two people who volunteered
    would make one call, which measures politeness. Four consecutive weeks
    measures use."

Three definitions this module commits to, because a gate whose terms are
argued after the fact is not a gate:

  1. A **publisher** is an identity with at least one ACCEPTED WRITE — a
     scope claim or a snapshot. Reading the feed is consuming, not
     publishing. the adoption gate counts publishers, so `GET /events` does not qualify.
  2. **Weekly** is the ISO week (Mon–Sun), keyed `YYYY-Www`. Rolling
     seven-day windows would let one busy Friday satisfy two "weeks".
  3. **Consecutive** means adjacent ISO weeks with no gap. A missed week
     resets the streak to zero rather than being bridged, because the gate
     measures sustained use and bridging is how a failing gate passes.

The operator's own identity is excluded by NAME, supplied by the caller. It
is not inferred from "whoever has the most calls" — that heuristic would
silently exclude the programme's first genuine power user.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta, timezone
from typing import Iterable, Optional

# The verbs that count as publishing. Closed, and deliberately excluding the
# read verb — see definition (1) above.
PUBLISHING_VERBS = ("claim_scope", "release_scope", "snapshot")

# The published SLO, restated here so the report can mark a breach without
# the reader having to hold the numbers in their head.
LATENCY_SLO_MS = {"claim_scope": 300.0, "release_scope": 300.0, "snapshot": 300.0, "events": 500.0}

# the scope-model decision (docs/decisions/0001): "The registry publishes its own precision weekly: conflicts fired ÷
# conflicts a holder acted on. Below a floor, the granularity rule is wrong
# and the fix is `k`, not more leases."
CONFLICT_PRECISION_FLOOR = 0.5

# ...but not on a sample of one. A single unacted-on conflict reads as
# precision 0.0 and would tell the operator to raise `k` on no evidence.
# the alarm-credibility rule generalises: "a marginal miss is the signature of a false
# positive and alarm credibility is a resource". Below this many fired
# conflicts the honest verdict is "not enough data", not a recommendation.
CONFLICT_PRECISION_MIN_SAMPLE = 10

# Paths that write to this registry WITHOUT producing a `calls` row, named
# with the reason — the named-gaps discipline ("an unmetered path the operator has
# never heard of is worse than one on a list"). Direct library use bypasses
# `app._handle`, which is the only caller of `record_call`. This is correct
# rather than a gap: the adoption gate counts colleagues publishing over HTTP, and the
# digest job's own writes are not somebody adopting the tool. It is listed so
# that a future in-process publisher is not silently uncounted.
UNMETERED_PATHS = (
    "agentco.leases / snapshots called directly as a library "
    "(server-side digest job, tests) — not an adopter, deliberately uncounted",
)


def _iso_week(value: str) -> str:
    at = datetime.fromisoformat(value)
    y, w, _ = at.isocalendar()
    return f"{y}-W{w:02d}"


def _week_key(day: date) -> str:
    y, w, _ = day.isocalendar()
    return f"{y}-W{w:02d}"


def _percentile(values: list[float], pct: float) -> Optional[float]:
    """Nearest-rank percentile. `None` for an empty sample — never 0.0.

    Same rule as the usage meter's the null-not-zero rule: an unreported number must never be
    readable as a measured zero. A p99 of 0 ms would look like the fastest
    endpoint in the building.
    """
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, int(round(pct / 100.0 * len(ordered))))
    return round(ordered[min(rank, len(ordered)) - 1], 2)


def weekly_active_publishers(
    conn: sqlite3.Connection,
    *,
    exclude: Iterable[str] = (),
    weeks: int = 8,
    now: Optional[datetime] = None,
) -> dict[str, list[str]]:
    """ISO week → sorted list of identities that published in it.

    Weeks with no publishers are present with an empty list rather than
    absent, so a streak calculation reads a gap as a gap. Omitting empty weeks
    is how "four consecutive weeks" quietly becomes "four weeks in which
    anything happened at all".
    """
    at = now or datetime.now(timezone.utc)
    excluded = {e.strip().lower() for e in exclude if e and e.strip()}
    placeholders = ",".join("?" for _ in PUBLISHING_VERBS)
    rows = conn.execute(
        f"SELECT actor, at FROM calls WHERE status = 'accepted' AND verb IN ({placeholders})",
        PUBLISHING_VERBS,
    ).fetchall()

    # ONE comparison for both halves. The exclusion folded case and the bucket
    # did not, so the gate — the single instrument that decides whether any of
    # this gets built — was satisfied by ONE person publishing as `Alice` and
    # `alice`: two rows, two "publishers", streak met. Reading the report, a
    # human sees two names that are obviously the same person and a verdict that
    # says the bar was cleared.
    #
    # Folding here is only safe because `auth.load_keys` now REFUSES a key file
    # containing two identities that differ solely by case. Without that, this
    # would merge two genuinely distinct authenticated actors, which is the
    # opposite error and just as wrong. The ambiguity is settled where identity
    # is configured, not where it is counted.
    buckets: dict[str, set[str]] = {}
    for row in rows:
        canonical = row["actor"].strip().lower()
        if canonical in excluded:
            continue
        buckets.setdefault(_iso_week(row["at"]), set()).add(canonical)

    # Materialise the trailing window, empty weeks included.
    out: dict[str, list[str]] = {}
    today = at.date()
    for i in range(weeks - 1, -1, -1):
        key = _week_key(today - timedelta(weeks=i))
        out[key] = sorted(buckets.get(key, set()))
    return out


def gate1_status(
    conn: sqlite3.Connection,
    *,
    operator: str,
    required_publishers: int = 2,
    consecutive_weeks: int = 4,
    now: Optional[datetime] = None,
) -> dict:
    """Is the adoption gate met? The whole gate, computed, with its own terms attached.

    The CURRENT week is excluded from the streak. A week in progress cannot
    yet be known to have met the bar, and counting it would let the gate pass
    on a Monday and fail on the following Sunday — a gate that oscillates is
    not a decision instrument.
    """
    at = now or datetime.now(timezone.utc)
    window = max(consecutive_weeks + 4, 8)
    weekly = weekly_active_publishers(conn, exclude=[operator], weeks=window, now=at)

    current = _week_key(at.date())
    completed = [(k, v) for k, v in weekly.items() if k != current]

    streak = 0
    best = 0
    for _, publishers in completed:
        if len(publishers) >= required_publishers:
            streak += 1
            best = max(best, streak)
        else:
            streak = 0

    return {
        "gate": "GATE-1",
        "criterion": (
            f"≥{required_publishers} identities other than {operator} are weekly active "
            f"publishers for {consecutive_weeks} consecutive ISO weeks"
        ),
        "met": streak >= consecutive_weeks,
        "currentStreakWeeks": streak,
        "longestStreakWeeks": best,
        "weeksRequired": consecutive_weeks,
        "operatorExcluded": operator,
        "weekInProgress": current,
        "byWeek": {k: v for k, v in completed},
        "definitions": {
            "publisher": "an identity with ≥1 accepted write (scope claim or snapshot); "
            "reading the feed does not qualify",
            "week": "ISO week, Monday–Sunday",
            "consecutive": "adjacent ISO weeks; a missed week resets the streak",
        },
    }


def time_to_first_event(conn: sqlite3.Connection) -> list[dict]:
    """Per identity: first contact → first accepted write, and the failures between.

    Time-to-first-published-event measures the *integration* cost.
    The count of refusals before the first success is the more actionable half
    — a colleague who was refused four times before their first accepted call
    experienced this registry as broken, and that is recoverable only if it is
    visible. An identity that has never succeeded is reported with
    `firstAcceptedAt: None`, never omitted.
    """
    rows = conn.execute("SELECT actor, verb, status, code, at FROM calls ORDER BY at ASC").fetchall()
    per: dict[str, dict] = {}
    for row in rows:
        entry = per.setdefault(
            row["actor"],
            {
                "actor": row["actor"],
                "firstContactAt": row["at"],
                "firstAcceptedAt": None,
                "refusalsBeforeFirstAccept": 0,
                "refusalCodes": [],
            },
        )
        if entry["firstAcceptedAt"] is not None:
            continue
        if row["status"] == "accepted" and row["verb"] in PUBLISHING_VERBS:
            entry["firstAcceptedAt"] = row["at"]
        elif row["status"] == "refused":
            entry["refusalsBeforeFirstAccept"] += 1
            if row["code"]:
                entry["refusalCodes"].append(row["code"])

    out = []
    for entry in per.values():
        if entry["firstAcceptedAt"]:
            delta = datetime.fromisoformat(entry["firstAcceptedAt"]) - datetime.fromisoformat(
                entry["firstContactAt"]
            )
            entry["secondsToFirstAccept"] = round(delta.total_seconds(), 1)
        else:
            entry["secondsToFirstAccept"] = None
        out.append(entry)
    return sorted(out, key=lambda e: e["firstContactAt"])


def verb_latency(
    conn: sqlite3.Connection,
    *,
    since: Optional[datetime] = None,
) -> list[dict]:
    """p50/p95/p99 server-side latency per verb, against the published SLO.

    This is "the only adoption cost the candidate did not measure — a
    support engineer paying 3 s × 40 triages a day will bypass, correctly, and
    nothing would have reported it."
    """
    sql = "SELECT verb, latency_ms, at FROM calls"
    args: list = []
    if since is not None:
        sql += " WHERE at >= ?"
        args.append(since.astimezone(timezone.utc).isoformat())
    rows = conn.execute(sql, args).fetchall()

    grouped: dict[str, list[float]] = {}
    for row in rows:
        grouped.setdefault(row["verb"], []).append(float(row["latency_ms"]))

    out = []
    for verb, values in sorted(grouped.items()):
        p99 = _percentile(values, 99)
        slo = LATENCY_SLO_MS.get(verb)
        out.append(
            {
                "verb": verb,
                "calls": len(values),
                "p50Ms": _percentile(values, 50),
                "p95Ms": _percentile(values, 95),
                "p99Ms": p99,
                "sloMs": slo,
                "breach": bool(slo is not None and p99 is not None and p99 > slo),
            }
        )
    return out


def conflict_precision(
    conn: sqlite3.Connection,
    *,
    since: Optional[datetime] = None,
) -> dict:
    """the scope-model decision's self-audit: conflicts fired ÷ conflicts a holder acted on.

    Below the floor, the fix is `k` (`scope.MIN_SEGMENTS`) — NOT more leases,
    and not a quieter notification. The verdict string says so, because the
    person reading this report in three months will not have the scope-model decision (docs/decisions/0001) open.
    """
    sql = "SELECT action, acted_at FROM conflict_actions"
    args: list = []
    if since is not None:
        sql += " WHERE fired_at >= ?"
        args.append(since.astimezone(timezone.utc).isoformat())
    rows = conn.execute(sql, args).fetchall()

    fired = len(rows)
    # "Acted on" means the holder changed course. Releasing a lease at the end
    # of the work you always intended to do is not acting on a conflict, so a
    # plain `released` does not count; narrowing or standing down does.
    acted = sum(1 for r in rows if r["action"] in ("narrowed", "released_due_to_conflict"))

    precision = round(acted / fired, 3) if fired else None
    if precision is None:
        verdict = "no conflicts fired yet — nothing to judge"
        actionable = False
    elif fired < CONFLICT_PRECISION_MIN_SAMPLE:
        # Deliberately refuses to recommend. Telling the operator to raise `k`
        # off two conflicts would spend the credibility this report needs the
        # first time it says something expensive.
        verdict = (
            f"{fired} conflict(s) fired — below the {CONFLICT_PRECISION_MIN_SAMPLE} needed "
            f"to judge granularity. Precision is reported, not actionable yet."
        )
        actionable = False
    elif precision >= CONFLICT_PRECISION_FLOOR:
        verdict = f"at or above the {CONFLICT_PRECISION_FLOOR} floor — granularity is holding"
        actionable = True
    else:
        verdict = (
            f"below the {CONFLICT_PRECISION_FLOOR} floor — per the scope-model decision (docs/decisions/0001) the granularity rule "
            f"is wrong and the fix is raising scope.MIN_SEGMENTS (currently k), not more leases"
        )
        actionable = True

    return {
        "conflictsFired": fired,
        "conflictsActedOn": acted,
        "precision": precision,
        "floor": CONFLICT_PRECISION_FLOOR,
        "minSample": CONFLICT_PRECISION_MIN_SAMPLE,
        "actionable": actionable,
        "verdict": verdict,
    }


def record_call(
    conn: sqlite3.Connection,
    *,
    verb: str,
    actor: str,
    status: str,
    latency_ms: float,
    code: Optional[str] = None,
    at: Optional[datetime] = None,
) -> None:
    """One row per request, refusals included. The whole of 1d depends on this.

    Deliberately never raises: a failure to record telemetry must not fail the
    call it is measuring (the same rule as the usage meter's the telemetry-never-fails-the-work rule). A lost
    metric row is a gap in a report; a failed publish is a lost colleague.
    """
    try:
        stamp = (at or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
        with conn:
            conn.execute(
                "INSERT INTO calls(verb, actor, status, code, latency_ms, at) VALUES (?,?,?,?,?,?)",
                (verb, actor, status, code, float(latency_ms), stamp),
            )
    except Exception:  # noqa: BLE001 - see docstring
        pass
