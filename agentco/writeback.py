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
            writer(notice)
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
        "notices": sent,
        "cursor": cursor,
        "via": via if via in _WRITERS else "webhook",
    }
