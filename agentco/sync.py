"""Follow the hub's feed, and keep a local copy of the procedures this harness may run.

**The gap this closes.** The plane has announced ASOP lifecycle on the change
feed since the work kinds joined it — `AsopVersioned`, `AsopActivated`,
`AsopRetired` — and the session hook already tells a session to call `events`
with a saved cursor. Nothing consumed any of it. A procedure could be authored,
activated and withdrawn centrally and no harness would ever act differently,
which makes the distribution half of the product a substrate with no client.

**Why a cursor and not a listing.** Asking "what procedures exist" costs the
plane a read of every record it holds, and every harness asking on a timer pays
that repeatedly for an answer that is usually "the same as last time". Following
the cursor costs an indexed read of the rows added since a position, so the
expensive read happens only when something actually changed. That is the whole
reason the kinds exist; this module is the half that collects on it.

**What it does NOT do.** It does not execute anything, and it does not decide
whether a procedure applies to the work in front of you. It synchronises a
cache, honestly, and says what changed. Enforcement is a separate concern living
at a different layer, for a reason worth stating: a component that both decides
and enforces is one that grades its own homework, which is the failure the gate
model exists to prevent.

**Fail posture: loud.** This raises. A caller that must not break — the
`SessionStart` hook is the one that matters, whose own docstring calls
fail-open "the entire design constraint" — wraps it. Baking silence in here
would take that choice away from every other caller, and a sync that failed
quietly would present as a harness confidently running a retired procedure.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

#: Beside the outbox, because they are the same thing: the harness's own local
#: state about its participation, in the directory the ladder already uses.
DEFAULT_ROOT = ".agentco"
CURSOR_FILE = "sync-cursor"
ASOP_DIR = "asops"


@dataclass
class SyncReport:
    """What one pass changed, in the words a person would use."""

    activated: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    retired: list[str] = field(default_factory=list)
    events_seen: int = 0
    cursor: Optional[str] = None
    unreadable: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.activated or self.updated or self.retired)

    def summary(self) -> str:
        if not self.events_seen:
            return "nothing new"
        parts = []
        for label, ids in (("activated", self.activated), ("updated", self.updated),
                           ("retired", self.retired)):
            if ids:
                parts.append(f"{len(ids)} {label}")
        if self.unreadable:
            parts.append(f"{len(self.unreadable)} unreadable")
        return f"{self.events_seen} event(s): " + (", ".join(parts) if parts else "none about procedures")


class AsopCache:
    """The procedures this harness holds, on disk, one file each.

    On disk rather than in memory because the consumer is a hook that runs at
    session start and exits: a cache that lived for the length of the process
    would be re-fetched every session, which is the listing this module exists
    to avoid.
    """

    def __init__(self, root: str | Path = DEFAULT_ROOT) -> None:
        self.root = Path(root)
        self.dir = self.root / ASOP_DIR
        self.cursor_path = self.root / CURSOR_FILE

    # -- the cursor ------------------------------------------------------

    def cursor(self) -> Optional[str]:
        try:
            value = self.cursor_path.read_text().strip()
        except OSError:
            return None
        return value or None

    def save_cursor(self, cursor: Optional[str]) -> None:
        if cursor is None:
            return
        self.root.mkdir(parents=True, exist_ok=True)
        # Written LAST by the caller, after the procedures it covers are on
        # disk. A cursor saved first, then interrupted, is a harness that has
        # permanently skipped the events between the two writes — and it would
        # look exactly like a harness that was up to date.
        tmp = self.cursor_path.with_suffix(".tmp")
        tmp.write_text(f"{cursor}\n")
        tmp.replace(self.cursor_path)

    # -- the procedures --------------------------------------------------

    def path_for(self, asop_id: str) -> Path:
        return self.dir / f"{asop_id}.json"

    def put(self, asop_id: str, record: dict) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.path_for(asop_id).with_suffix(".tmp")
        tmp.write_text(json.dumps(record, indent=2, sort_keys=True))
        tmp.replace(self.path_for(asop_id))

    def drop(self, asop_id: str) -> bool:
        try:
            self.path_for(asop_id).unlink()
            return True
        except OSError:
            return False

    def get(self, asop_id: str) -> Optional[dict]:
        try:
            return json.loads(self.path_for(asop_id).read_text())
        except (OSError, json.JSONDecodeError):
            return None

    def held(self) -> list[str]:
        if not self.dir.is_dir():
            return []
        return sorted(p.stem for p in self.dir.glob("*.json"))


class Sync:
    """One pass of the feed, applied to one cache."""

    #: Only these change what a harness may run. Every other kind on the feed
    #: is somebody else's business and is counted, not acted on — a client that
    #: reacted to kinds it did not understand would be a client that breaks
    #: when the vocabulary grows, and the vocabulary is meant to grow.
    ACTS_ON = ("AsopActivated", "AsopVersioned", "AsopRetired")

    def __init__(self, registry: Any, cache: Optional[AsopCache] = None, *, limit: int = 200):
        self.registry = registry
        self.cache = cache or AsopCache()
        self.limit = limit

    def once(self) -> SyncReport:
        report = SyncReport()
        page = self.registry.events(since=self.cache.cursor(), limit=self.limit)
        events = page.get("events") or []
        report.events_seen = len(events)

        for event in events:
            kind = event.get("kind")
            if kind not in self.ACTS_ON:
                continue
            payload = event.get("payload") or {}
            asop_id = payload.get("asopId")
            if not asop_id:
                continue
            if kind == "AsopRetired":
                if self.cache.drop(asop_id):
                    report.retired.append(asop_id)
                continue
            # Activated or a new version: fetch THAT ONE, not the library.
            try:
                fetched = self.registry.sop_get(asop_id)
            except Exception:  # noqa: BLE001 - recorded, not swallowed; see below
                # One unreadable procedure must not strand the cursor, or a
                # single bad record blocks every later event forever. It is
                # named in the report so the caller can see it rather than
                # infer it from a count that did not move.
                report.unreadable.append(asop_id)
                continue
            record = fetched.get("sop") if isinstance(fetched, dict) else None
            if not record:
                # An activation the reader cannot see is not an error: the
                # plane serves the ACTIVE version, and a draft revision is
                # announced before anything is active to fetch.
                continue
            self.cache.put(asop_id, record)
            (report.activated if kind == "AsopActivated" else report.updated).append(asop_id)

        report.cursor = page.get("nextCursor")
        # Last, deliberately — see `save_cursor`.
        self.cache.save_cursor(report.cursor)
        return report


def registry_from_env() -> Any:
    """Build a client from the same variables the MCP remote backend reads.

    Refuses rather than defaulting: a sync pointed at the wrong plane is worse
    than one that did not run, because it would fill the cache with another
    team's procedures and nothing downstream would question them.
    """
    from agentco.publish import Registry

    url = os.environ.get("AGENTCO_REGISTRY_URL")
    actor = os.environ.get("AGENTCO_ACTOR")
    secret = os.environ.get("AGENTCO_SECRET")
    missing = [name for name, value in (
        ("AGENTCO_REGISTRY_URL", url), ("AGENTCO_ACTOR", actor), ("AGENTCO_SECRET", secret)
    ) if not value]
    if missing:
        raise RuntimeError(
            "cannot sync: " + ", ".join(missing) + " not set. "
            "A harness syncs procedures from the hub it is a worker on — "
            "`agentco keygen <actor>` mints the secret, and the operator names the URL."
        )
    return Registry(actor, secret, url)
