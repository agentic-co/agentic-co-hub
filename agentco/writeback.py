"""Telling the record a piece of work came from that its gate is waiting.

A parked human gate reaches nobody on its own. The change feed now carries
`WorkParked` and `GateEscalated` (`agentco/events.py`), which is the substrate;
this is the one subscriber that closes the loop back to wherever the work
originated — the Azure DevOps work item, the Jira issue, the row in whatever
system of record filed it.

**This is the narrow, opt-in exception to "AgentCo never writes to your system
of record", and it is stated rather than quietly taken.** The promise is what
makes adoption safe: nothing here can damage the tool you already trust. So the
exception is bounded on every side that matters —

  * **Off unless configured.** No writer, no writes, and the pass says so.
  * **Notices only.** One shape, `{itemId, sourceId, kind, title, waitedSeconds,
    dueAt, check, assignedTo}`. There is no path from here to changing a state,
    closing a ticket, or editing a field. A connector that wants to do more is
    writing its own integration, not extending this one.
  * **Only for work that HAS an origin.** An item filed locally has nowhere to
    write back to and is skipped rather than errored.
  * **AgentCo does not speak your vendor's API.** The built-in writer POSTs the
    notice to a URL you control, exactly as `delivery` posts a digest — so the
    thing that actually writes to Azure DevOps is your code, holding your
    credential, applying your rules. That is not a technicality: it keeps the
    blast radius inside the org that chose it, and it means this module cannot
    grow an opinion about what a work item looks like.

A connector wanting to write directly registers a writer, the same shape
`delivery.register_sender` uses:

    from agentco import writeback

    def to_ado(notice: dict) -> None:
        ...

    writeback.register_writer("ado", to_ado)

**Idempotency is the cursor.** The feed is resumable and this reads it like any
other subscriber, persisting where it got to. A pass that re-notified from the
beginning would put the same comment on the same ticket every five minutes,
which is how a channel becomes one people filter.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Optional

from agentco import events

WRITEBACK_URL_ENV_VAR = "AGENTCO_WRITEBACK_URL"
CURSOR_ENV_VAR = "AGENTCO_WRITEBACK_CURSOR"
DEFAULT_CURSOR_FILE = "~/.agentco/writeback.cursor"
DEADLETTER_ENV_VAR = "AGENTCO_WRITEBACK_DEADLETTER"
DEFAULT_DEADLETTER_FILE = "~/.agentco/writeback.deadletter.jsonl"

# The only kinds that travel. Adding one here is the decision to notify about
# something new, and it should look like a decision in a diff.
NOTIFIED_KINDS = ("WorkParked", "GateEscalated")

Writer = Callable[[dict], None]

_WRITERS: dict[str, Writer] = {}


class WritebackNotConfigured(RuntimeError):
    """No writer and no URL. Raised rather than defaulted — see the docstring."""


class WritebackFailed(RuntimeError):
    def __init__(self, status: Optional[int], detail: str):
        self.status = status
        super().__init__(f"write-back failed (HTTP {status or '?'}): {detail}")


def register_writer(name: str, writer: Writer) -> None:
    _WRITERS[name] = writer


def writeback_url() -> Optional[str]:
    url = os.environ.get(WRITEBACK_URL_ENV_VAR)
    return url.strip() or None if url else None


def notice_from(event: dict) -> Optional[dict]:
    """The narrow shape a connector receives, or None if there is nowhere to write.

    Built from the event rather than by re-reading the work item, so a
    connector cannot be handed fields nobody decided to send. `sourceId` is what
    ties this to a record in somebody else's system; without it there is no
    origin to notify and the event is not ours to act on.
    """
    payload = event.get("payload") or {}
    source_id = payload.get("sourceId")
    if not source_id:
        return None
    return {
        "kind": event["kind"],
        "itemId": payload.get("itemId"),
        "source": payload.get("source"),
        "sourceId": source_id,
        "title": payload.get("title"),
        "check": payload.get("check"),
        "assignedTo": payload.get("assignedTo") or payload.get("to"),
        "dueAt": payload.get("dueAt"),
        "waitedSeconds": payload.get("waitedSeconds"),
        "occurredAt": event.get("occurredAt"),
    }


def post_json(notice: dict, url: Optional[str] = None, timeout: int = 20) -> None:
    """The honest default: POST the notice to a URL the operator controls.

    What turns this into a comment on a work item is the endpoint's code, not
    this function. AgentCo holds no vendor credential and knows no vendor's
    schema, which is the same boundary `delivery.post_json` draws and the reason
    the exception to the no-writes promise stays as small as it does.
    """
    target = url or writeback_url()
    if not target:
        raise WritebackNotConfigured(
            f"no write-back destination. Set {WRITEBACK_URL_ENV_VAR} to an endpoint "
            f"you control, or register a writer — this feature is off until one of "
            f"those exists, on purpose."
        )
    request = urllib.request.Request(
        target,
        data=json.dumps(notice).encode(),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if response.status >= 300:  # pragma: no cover - urllib raises first
                raise WritebackFailed(response.status, "unexpected status")
    except urllib.error.HTTPError as exc:
        raise WritebackFailed(exc.code, exc.reason or "") from exc
    except urllib.error.URLError as exc:
        raise WritebackFailed(None, f"cannot reach {target}: {exc.reason}") from exc


def _cursor_path() -> Path:
    return Path(os.environ.get(CURSOR_ENV_VAR) or DEFAULT_CURSOR_FILE).expanduser()


def read_cursor() -> Optional[str]:
    path = _cursor_path()
    if not path.exists():
        return None
    value = path.read_text("utf-8").strip()
    return value or None


def write_cursor(cursor: str) -> None:
    path = _cursor_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(cursor, encoding="utf-8")


# Four 4xx statuses that are not this caller's fault forever, so they are
# excluded from `_is_permanent_failure` rather than dead-lettered with the
# rest of the class. 408 and 429 are the network's fault today — a timeout or
# a rate limit says try again later, not that the ticket is gone. 401 and 403
# are the operator's to fix (rotate the token, grant access) and then they
# want the notice replayed, not silently dropped because the credential that
# was wrong at the time got dead-lettered along with tickets that no longer
# exist.
TRANSIENT_4XX = frozenset({401, 403, 408, 429})


def _is_permanent_failure(exc: BaseException) -> bool:
    """A 4xx is the caller's fault forever, not the network's fault today —
    except the four in `TRANSIENT_4XX`, which are exactly the opposite case.

    The ticket was deleted, the URL was never valid — retrying the same POST
    does not fix either of those, so the rest of the 4xx range is permanent.
    `status` is only ever set by `WritebackFailed`; any other exception has no
    verdict to give and is treated as transient, which is the safer default
    when we cannot tell.
    """
    status = getattr(exc, "status", None)
    if not isinstance(status, int):
        return False
    return 400 <= status < 500 and status not in TRANSIENT_4XX


def _deadletter_path() -> Path:
    return Path(os.environ.get(DEADLETTER_ENV_VAR) or DEFAULT_DEADLETTER_FILE).expanduser()


def _write_deadletter(notice: dict, exc: BaseException) -> None:
    """Record a notice this pass gave up on, rather than retry it forever.

    Appended, never overwritten — the cursor already moved past this event,
    so the file is the only remaining record that the notice was dropped
    rather than delivered.
    """
    path = _deadletter_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "notice": notice,
        "status": getattr(exc, "status", None),
        "detail": str(exc),
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


def run(
    conn,
    *,
    via: str = "webhook",
    since: Optional[str] = None,
    dry_run: bool = False,
    limit: int = 200,
) -> dict:
    """Read the feed forward and notify each origin once. Never raises on config.

    An unconfigured pass is the DEFAULT state and reports itself as one — a
    scheduled job that failed loudly because an optional feature was switched
    off is a job whose owner turns the alerting off, and then misses the failure
    that mattered.

    The cursor advances only over events that were actually handled, so a
    delivery failure is retried on the next run instead of being skipped by a
    watermark that moved regardless.

    A writer exception splits two ways. A **permanent** failure (4xx, other
    than the four in `TRANSIENT_4XX` — the ticket was deleted, the URL never
    existed) cannot be fixed by sending the same POST again, so that one
    notice is dead-lettered (`_write_deadletter`) and the pass moves on to the
    events after it. A **transient** failure (5xx, unreachable, a 401/403/
    408/429, or anything else we cannot classify) stops the pass — but the
    cursor is persisted up to the last event actually handled BEFORE the
    exception propagates, so the next run retries the poison event instead of
    everything from the beginning. Persisting before re-raising is the fix:
    previously the only write happened after the whole loop, so an exception
    on the second of three events left the cursor at `None` forever and
    re-sent the first notice on every subsequent run, without ever reaching
    the third.

    A dead-letter write can itself fail — an unwritable directory, a full
    disk. That must not be silent about the notice it was trying to record:
    it falls through to the same transient handling (cursor persisted up to
    the event before this one, so the poison event is retried rather than
    skipped) and raises a `WritebackFailed` naming the dead-letter path,
    rather than let the write's own `OSError`, unannotated, be the only trace
    that a permanent failure went unrecorded.
    """
    configured = via in _WRITERS or writeback_url() is not None
    if not configured:
        return {
            "state": "not-configured",
            "sent": 0,
            "skipped": 0,
            "detail": (
                f"write-back is off. Set {WRITEBACK_URL_ENV_VAR}, or register a "
                f"writer, to let a parked gate reach the record it came from."
            ),
        }

    start = since if since is not None else read_cursor()
    page = events.read(conn, since=start, limit=limit)
    writer = _WRITERS.get(via) or post_json

    sent: list[dict] = []
    skipped: list[str] = []
    deadlettered: list[dict] = []
    cursor = start
    for event in page["events"]:
        if event["kind"] not in NOTIFIED_KINDS:
            cursor = events.encode_cursor(event["seq"])
            continue
        notice = notice_from(event)
        if notice is None:
            # Filed locally, so there is no origin to write back to. Not an
            # error and not a failure to deliver — there was nothing addressed.
            skipped.append(event["uid"])
            cursor = events.encode_cursor(event["seq"])
            continue
        if not dry_run:
            try:
                writer(notice)
            except Exception as exc:
                if _is_permanent_failure(exc):
                    try:
                        _write_deadletter(notice, exc)
                    except OSError as deadletter_exc:
                        # The dead-letter write is what turns "permanent
                        # failure" into "handled". If IT fails too — an
                        # unwritable directory, a full disk — the notice is
                        # now neither delivered nor recorded, and raising
                        # `deadletter_exc` bare would be silent about that:
                        # the caller sees an OSError with no notice attached
                        # and no reason to suspect the poison event is still
                        # sitting there un-dead-lettered. So this falls
                        # through to exactly the transient handling below —
                        # persist the cursor up to the event before this one,
                        # so the next run retries rather than skips it — and
                        # raises a fresh error that names both the notice and
                        # the path that could not take it, rather than let a
                        # second WritebackFailed on the SAME event masquerade
                        # as the writer's original failure.
                        if cursor and cursor != start:
                            write_cursor(cursor)
                        raise WritebackFailed(
                            None,
                            f"permanent failure on {notice.get('sourceId')!r} "
                            f"could not be dead-lettered to "
                            f"{_deadletter_path()}: {deadletter_exc}",
                        ) from deadletter_exc
                    deadlettered.append(notice)
                    # Per EVENT, from its seq — see the comment below. A
                    # permanent failure is handled, just not delivered, so it
                    # advances the cursor exactly like a sent notice does.
                    cursor = events.encode_cursor(event["seq"])
                    continue
                # Transient: persist everything handled so far, then let the
                # caller see the failure. Doing this here, not after the
                # loop, is what makes the retry start at THIS event instead
                # of at the beginning.
                if cursor and cursor != start:
                    write_cursor(cursor)
                raise
        sent.append(notice)
        # Per EVENT, from its seq — `events.read` returns a page-level
        # `nextCursor` and no per-row one, so reaching for `event["cursor"]`
        # silently never advanced the watermark and every run re-notified from
        # the beginning. Advancing per handled event is also what makes a
        # delivery failure retry instead of being skipped by a watermark that
        # moved regardless.
        cursor = events.encode_cursor(event["seq"])

    if not dry_run and cursor and cursor != start:
        write_cursor(cursor)

    return {
        "state": "dry-run" if dry_run else "sent",
        "sent": len(sent),
        "skipped": len(skipped),
        "deadlettered": len(deadlettered),
        "notices": sent,
        "cursor": cursor,
        "via": via if via in _WRITERS else "webhook",
    }
