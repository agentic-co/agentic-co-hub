"""Defects found by adversarial review, each as a test of the property that SHOULD hold.

Every test in this file **fails against the current code**. Each is marked
`xfail(strict=True)`, which means two things and both are deliberate:

  * the suite stays green while the defect stands, so this file can be merged
    without blocking anything;
  * the suite goes RED the moment a defect is fixed and the marker is not
    removed. A silently-passing xfail is how a fixed bug stops being tracked,
    which is the same silent-success failure mode the codebase refuses
    everywhere else. Fix the defect, delete the marker, in one change.

The names state the property that should hold, never the bug. If you are
reading one of these because it started failing as an XPASS: that is the
system working. Remove the marker.

**Why a separate file.** These are not confirmations of the design; they are
counterexamples to it. Several of them sit directly beside an existing test
that asserts the same-sounding property and passes — because that test
exercises the half of the property that holds. Each docstring below names the
neighbouring test and says exactly which half it misses. That comparison is
the most useful thing here: the defects are cheap to fix, the testing habit
that hid them is not.

Nothing in this file imports a fix or asserts a particular implementation. Where
a defect has more than one honest resolution the assertion is written to accept
any of them, and the docstring says so.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import threading
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agentco import auth, db, metrics, snapshots
from agentco.app import create_app
from agentco.errors import Refusal, Unauthenticated
from agentco.scope import Scope, prefixes_overlap, scopes_intersect, validate_prefix
from agentco.sop import SopLibrary
from agentco.work import LeaseError, Queue, WorkStatus

NOW = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)

KEYS = {"dana": "dana-secret", "kofi": "kofi-secret", "operator": "op-secret"}


@pytest.fixture()
def queue(tmp_path):
    return Queue(tmp_path / "work.jsonl")


@pytest.fixture()
def library(tmp_path):
    return SopLibrary(tmp_path / "sops.jsonl")


@pytest.fixture()
def client(tmp_path):
    app = create_app(db_path=str(tmp_path / "api.sqlite3"), keys=KEYS, operator="operator")
    return TestClient(app)


def signed(method: str, path: str, actor: str, body: dict | None = None) -> dict:
    raw = json.dumps(body).encode() if body is not None else b""
    ts = str(int(time.time()))
    return {
        "X-AgentCo-Actor": actor,
        "X-AgentCo-Timestamp": ts,
        "X-AgentCo-Signature": auth.sign(KEYS[actor], method, path, ts, raw),
        "Content-Type": "application/json",
    }


def post(client, path, actor, body):
    return client.post(path, content=json.dumps(body), headers=signed("POST", path, actor, body))


# --------------------------------------------------------------------------- #
# Store integrity — one bad row must not be fatal, and must not be erased
# --------------------------------------------------------------------------- #


def test_one_undecodable_byte_does_not_take_the_whole_queue_down(queue):
    """A single non-UTF-8 byte anywhere in the store makes EVERY operation raise.

    `Queue._read_raw` wraps only `json.loads` in its quarantine `try`. The
    decode happens one line earlier, at `self.path.read_text(encoding="utf-8")`,
    which raises `UnicodeDecodeError` — a subclass of `ValueError`, not of
    `json.JSONDecodeError` and not of `OSError`. Nothing catches it, so `get`,
    `list`, `ready`, `create` and `claim` all fail together. The store is not
    degraded, it is down.

    This is the same exception-type escape shape that has bitten this author's
    codebase before: a per-item handler that names too narrow a class, and a
    broader one arriving from the layer underneath.

    Two sibling inputs reach the same fatal end by the same route, and a fix
    should cover all three:
      * `{"status": "cancelled"}` from a newer writer — `WorkStatus(...)` in
        `WorkItem.from_json` raises `ValueError`;
      * `{"id": "w-partial"}` — `cls(**data)` raises `TypeError` for the
        missing `title`.
    Note `SopLibrary._read_all` DOES catch `(ValueError, TypeError)` per line
    while `Queue._read_raw` does not, so the two stores currently disagree
    about their own quarantine contract.

    Why the existing test misses it: `test_a_corrupt_line_is_quarantined_not_fatal_and_not_dropped`
    appends `"{not json at all\\n"` — valid UTF-8 that is invalid JSON. That
    input exercises the one exception the handler names. Every fixture in the
    suite is written through `_write_all`, which cannot emit a byte sequence
    that fails to decode, so the suite is structurally unable to produce the
    input this bug needs.
    """
    good = queue.create("a real item")
    with queue.path.open("ab") as handle:
        handle.write(b'{"id": "w-latin1", "title": "caf\xe9"}\n')

    items = queue.list()

    assert [item.id for item in items] == [good.id]


def test_a_quarantined_line_survives_the_next_write(queue):
    """"Quarantine" is deletion. The line is reported once, then destroyed.

    `_read_raw` collects unparseable lines into `self.quarantined` and keeps
    them OUT of `raw_rows`. Every mutation then calls `_write_all(raw_rows)`,
    which rewrites the whole file from the survivors. One ordinary `create()`
    is enough: the corrupt line — which may be the only remaining record of a
    real piece of work — is gone from disk permanently, and `self.quarantined`
    is reset on the next read, so even the in-memory report does not survive.

    `work.py:214-219` states the opposite: "it must not vanish either — it is
    kept in `quarantined` so a health check can report it. Silently skipping
    bad data is how a queue loses work and nobody finds out for a fortnight."

    The same mechanism in `SopLibrary` is what makes a pinned SOP version
    resolvable to different text — see
    `test_a_pinned_sop_version_never_resolves_to_different_text` below.

    Why the existing test misses it: `test_a_corrupt_line_is_quarantined_not_fatal_and_not_dropped`
    asserts `queue.quarantined == ["{not json at all"]` immediately after a
    read and stops there. It never performs a subsequent write and never
    re-reads the file, so "not dropped" is checked against an in-memory list
    rather than against disk — which is the only place it could be dropped
    from.
    """
    queue.create("a real item")
    corrupt = '{"id": "w-corrupt", "title": "REAL WORK, truncated mid-write"'
    with queue.path.open("a", encoding="utf-8") as handle:
        handle.write(corrupt + "\n")

    queue.list()
    # `quarantined` holds raw BYTES: a line that failed to decode has no faithful
    # string form, and re-encoding a guess at it is how the original bytes get
    # lost on the next write. Only this precondition's type changed with the fix;
    # the assertion that matters, below, is untouched.
    assert queue.quarantined == [corrupt.encode()], "precondition: the line is quarantined on read"

    queue.create("an ordinary second item")

    on_disk = queue.path.read_text(encoding="utf-8").splitlines()
    assert corrupt in on_disk, (
        "the quarantined line was erased from disk by an unrelated write; "
        "quarantine must preserve, not delete"
    )


# --------------------------------------------------------------------------- #
# The lease fence — the integer is not enough
# --------------------------------------------------------------------------- #


@pytest.mark.xfail(
    strict=True,
    reason="report_result never bumps lease_attempt, so the same attempt number stays "
    "valid forever and a second report overwrites the first",
)
def test_a_repeated_report_at_the_same_attempt_is_refused(queue):
    """A second report at an already-reported attempt silently overwrites a real result.

    `report_result` clears `leased_by` and `lease_expires_at` but leaves
    `lease_attempt` unchanged. The reporter therefore no longer holds the item
    — and the identical call still succeeds, because the fence compares only
    the integer, which is still current. The queue accepts an unbounded number
    of terminal reports for one attempt, last writer winning.

    This is verbatim the failure `work.py:15-21` says the fence closes:
    "Accepting that late report would overwrite a real result with one derived
    from an execution the queue already abandoned. So every report is fenced on
    the attempt number it was issued under, and a mismatch **raises** and writes
    nothing." The mismatch case does raise. The MATCH-after-completion case is
    the one that is not covered, and it is the same abandoned-worker scenario.

    Related, and not separately tested here: `report_result` takes no `agent`
    argument at all, so the fence has no identity binding. `attempt` is a small
    integer starting at 1, and `metadata.lease_report.reported_by` is filled in
    from `item.leased_by` — so a forged report is recorded under the name of the
    legitimate holder.

    Why the existing tests miss it: `test_a_report_from_a_superseded_lease_is_refused_and_writes_nothing`
    and `test_a_stale_report_raises_rather_than_returning_none` both report
    against a DIFFERENT attempt than the current one. `test_the_attempt_counter_survives_completion`
    asserts the counter is preserved after a report but never sends a second
    report to find out what that preservation permits.
    """
    item = queue.create("build the thing")
    claimed = queue.claim(item.id, "worker-a", now=NOW)

    queue.report_result(item.id, claimed.lease_attempt, WorkStatus.DONE, result="the real result")

    with pytest.raises(LeaseError):
        queue.report_result(
            item.id, claimed.lease_attempt, WorkStatus.FAILED, result="a late overwrite"
        )
    assert queue.get(item.id).result == "the real result"


@pytest.mark.xfail(
    strict=True,
    reason="reap_expired_leases does not bump lease_attempt, so the revoked holder's "
    "attempt number is still current until somebody else claims",
)
def test_a_reap_alone_fences_out_the_holder_it_revoked(queue):
    """The reaper revokes a lease without invalidating the token that lease issued.

    `reap_expired_leases` returns the item to PENDING and clears `leased_by`,
    but leaves `lease_attempt` untouched. The worker whose lease was just
    revoked still holds a token that matches, so its report is accepted. The
    fence does not engage until a LATER claim bumps the counter — which may
    never happen, and certainly has not happened at the moment the reaped
    worker comes back.

    `work.py:564-573` claims the opposite in as many words: "The item goes back
    to PENDING with `lease_attempt` intact — the attempt counter is what makes
    the next report from the old holder get fenced out." The counter being
    intact is precisely why it does not.

    Why the existing test misses it: `test_a_reaped_item_fences_out_its_old_holder`
    calls `queue.claim(item.id, "worker-b", now=later)` between the reap and the
    report. Delete that one line and it fails. It demonstrates that the
    RE-CLAIM fences, and attributes the property to the reap. This test is that
    test with the re-claim removed, which is the scenario the docstring
    describes.
    """
    item = queue.create("build the thing")
    stale = queue.claim(item.id, "worker-a", ttl_seconds=60, now=NOW)
    later = NOW + timedelta(hours=2)

    queue.reap_expired_leases(now=later)
    # Deliberately NO re-claim here. The revoked holder is the next caller.

    with pytest.raises(LeaseError):
        queue.report_result(
            item.id, stale.lease_attempt, WorkStatus.DONE, result="a late report after revocation"
        )


@pytest.mark.xfail(
    strict=True,
    reason="reap_expired_leases reads self.list() outside the lock and its release "
    "closure never re-checks lease_active_at, so it writes over a lease taken "
    "after the snapshot",
)
def test_the_reaper_does_not_revoke_a_lease_that_went_live_mid_sweep(queue, monkeypatch):
    """The reaper acts on a stale snapshot and can revoke a healthy, live lease.

    `reap_expired_leases` iterates `self.list(WorkStatus.IN_PROGRESS)` OUTSIDE
    the advisory lock, then calls `_mutate` once per item. The `release` closure
    re-reads the target inside the lock — but only to get an object to mutate;
    it never re-checks `lease_active_at`. It writes PENDING unconditionally.

    So a worker that claims the item in the window between the reaper's read and
    the reaper's write loses its lease while still holding it and still working.
    A third worker can then claim, and the second worker's eventually-correct
    report is fenced out as "superseded" — for work that was never superseded by
    anything. The item is executed twice and one real result is discarded.

    The interleaving is forced deterministically by patching the public `list`
    to let a competing claim land after the snapshot is taken, which is exactly
    what a second process does on its own schedule. `claim` does not call
    `list`, so there is no re-entrancy, and no lock is held at that point, so
    there is no deadlock.

    Why the existing test misses it: `test_reaping_leaves_a_live_lease_alone`
    asserts `reap_expired_leases(...) == []` for a lease that is live at the
    moment of the OUTER read. When the outer read sees an expired lease the
    early `continue` never fires, and nothing downstream looks at expiry again —
    so the guard the test checks is not the guard that would have to hold.
    """
    item = queue.create("build the thing")
    queue.claim(item.id, "worker-a", ttl_seconds=60, now=NOW)
    later = NOW + timedelta(hours=2)

    original_list = Queue.list
    competing_claim_made = {"done": False}

    def list_then_someone_else_claims(self, status=None):
        snapshot = original_list(self, status)
        if not competing_claim_made["done"]:
            competing_claim_made["done"] = True
            # Another process claims the item AFTER the reaper has listed it as
            # expired but BEFORE the reaper's own write lands.
            queue.claim(item.id, "worker-b", ttl_seconds=3600, now=later)
        return snapshot

    monkeypatch.setattr(Queue, "list", list_then_someone_else_claims)
    queue.reap_expired_leases(now=later)
    monkeypatch.setattr(Queue, "list", original_list)

    after = queue.get(item.id)
    assert after.leased_by == "worker-b", "the reaper revoked a lease that was live when it wrote"
    assert after.status == WorkStatus.IN_PROGRESS


# --------------------------------------------------------------------------- #
# Dependencies — filed and never runnable
# --------------------------------------------------------------------------- #


@pytest.mark.xfail(
    strict=True,
    reason="nothing anywhere transitions BLOCKED -> PENDING; create() is the only "
    "writer of WorkStatus.BLOCKED and there is no reader that clears it",
)
def test_a_blocked_item_becomes_claimable_once_its_blocker_is_done(queue):
    """An item with `blocked_by` is filed and can never run.

    `create` parks an item with dependencies in `BLOCKED`. No code path in the
    package moves it out again — `grep -n "WorkStatus.BLOCKED" agentco/*.py`
    returns exactly one line, the setter. `ready()` skips anything that is not
    PENDING or IN_PROGRESS, and `claim()` refuses BLOCKED as terminal. The
    dependency graph the field implies is never evaluated against real state.

    `ready()` does compute a `done` set and filter `blocked_by` against it, so
    the intent is clearly that dependencies resolve — but that filter is
    unreachable for exactly the items it was written for, because they never
    reach a status `ready()` will consider.

    Why the existing test misses it: `test_a_blocked_item_becomes_ready_when_its_blocker_is_done`
    performs the missing transition by hand —
    `queue._mutate(blocked.id, lambda i: {"status": WorkStatus.PENDING})` —
    with a comment excusing it ("It was parked in BLOCKED at creation;
    readiness is about dependencies"). It tests the `blocked_by` filter inside
    `ready()`, which works. The gap is that nothing in production ever puts an
    item into the state that test constructs. This test is that test with the
    hand-written transition removed.
    """
    blocker = queue.create("the blocker")
    blocked = queue.create("the dependent item", blocked_by=[blocker.id])

    claimed = queue.claim(blocker.id, "worker-a", now=NOW)
    queue.report_result(blocker.id, claimed.lease_attempt, WorkStatus.DONE, result="done")

    assert blocked.id in {i.id for i in queue.ready(now=NOW)}


# --------------------------------------------------------------------------- #
# GATE-1 — the one number that decides whether the architecture gets built
# --------------------------------------------------------------------------- #


@pytest.mark.xfail(
    strict=True,
    reason="weekly_active_publishers excludes the operator case-insensitively but "
    "buckets publishers by the raw actor string, so two casings count as two people",
)
def test_gate1_does_not_count_one_identity_twice_for_a_change_of_case(tmp_path):
    """One person publishing under two capitalisations satisfies "two identities".

    `metrics.weekly_active_publishers` normalises on the way IN to the exclusion
    check — `{e.strip().lower() for e in exclude}` compared against
    `row["actor"].strip().lower()` — and then adds the RAW `row["actor"]` to the
    week's set. So `Dana` and `dana` are one identity for the purpose of
    excluding the operator and two identities for the purpose of counting
    publishers. Five weeks of one person alternating capitalisation reports
    `met: True`.

    This is the gate `docs/roadmap.md` describes as the only one that decides
    whether the architecture gets built, and `metrics.py:10-32` argues at
    length that it is "deliberately hard to game". The registry's own auth
    layer looks actors up case-SENSITIVELY (`auth.authenticate` does
    `table.get(actor)`), so the inconsistency is internal to this module rather
    than inherited.

    Setup note for anyone re-running this by hand: the operator must be a THIRD
    party. Excluding `Dana` as the operator folds case and removes both
    spellings, which reports zero publishers and hides the defect. The bug needs
    an operator who is not the doubled identity.

    Why the existing tests miss it: `test_gate1_needs_two_non_operator_publishers_for_four_consecutive_weeks`
    and `test_the_operator_does_not_count_towards_his_own_gate` both use actor
    names that are already distinct and already lowercase, so the raw value and
    the normalised value are the same string and the two code paths cannot
    disagree.
    """
    conn = db.connect(tmp_path / "gate.sqlite3")
    for weeks_ago in range(1, 6):
        for spelling in ("Dana", "dana"):
            metrics.record_call(
                conn,
                verb="snapshot",
                actor=spelling,
                status="accepted",
                latency_ms=1.0,
                at=NOW - timedelta(weeks=weeks_ago),
            )

    status = metrics.gate1_status(conn, operator="operator", now=NOW)

    assert status["met"] is False, (
        f"one identity under two casings satisfied the gate: {status['byWeek']}"
    )


# --------------------------------------------------------------------------- #
# The SOP pin — an instance must always resolve to the text it ran against
# --------------------------------------------------------------------------- #


@pytest.mark.xfail(
    strict=True,
    reason="a quarantined SOP row is deleted by the next write, and revise() then "
    "reissues the freed version number to different text",
)
def test_a_pinned_sop_version_never_resolves_to_different_text(library, tmp_path):
    """A pin keeps its number while the text underneath it is replaced.

    Two mechanisms compose. `SopLibrary._read_all` quarantines any row it cannot
    parse; `_write_all` then rewrites the file from the survivors only, so the
    quarantined version is destroyed by the next mutation. `revise()` computes
    the next version as `max(surviving versions) + 1`, which REISSUES the number
    that was just destroyed — to completely different text.

    The instance's `sop_ref` is untouched throughout. It still says version 2.
    Version 2 now says something else. `outcomes_by_version` will attribute the
    instance's outcome to text it never ran against, which `sop.py:22-25` says
    is the exact failure versioning exists to prevent: "every number computed
    from it would be fiction".

    Between the corruption and the next `revise()` the pin is simply
    unresolvable — `get(sop_id, 2)` returns `None` — which contradicts
    `sop.py:107-109` ("instances pinned to this version must stay resolvable
    forever") and `:277-278` ("Deleting it would orphan every instance pinned to
    it"). The code performs that deletion.

    The trigger used here is a status value a newer writer might store, which is
    the forward-compatibility case `WorkItem.from_json` is explicitly designed
    for elsewhere. Any unparseable row does the same thing.

    Why the existing tests miss it: `test_a_superseded_version_stays_resolvable_forever`
    and `test_a_revision_does_not_reach_back_into_a_running_instance` both
    operate on a store where every row round-trips cleanly. No test in
    `test_sop.py` writes a row this version cannot parse, so the quarantine
    branch is never entered and the version-number reissue it enables is never
    reachable.
    """
    sop = library.create("Deploy the service", purpose="the original v1 procedure")
    library.revise(sop.sop_id, purpose="the v2 procedure: run the smoke tests first")
    library.activate(sop.sop_id, 2)

    queue = Queue(tmp_path / "work.jsonl")
    item = library.instantiate(sop.sop_id, queue, title="deploy run")
    pinned = item.metadata["sop_ref"]["version"]
    assert library.get(sop.sop_id, pinned).purpose == "the v2 procedure: run the smoke tests first"

    # A newer writer stores a status this version does not know about.
    rewritten = []
    for line in library.path.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        if row["version"] == pinned:
            row["status"] = "retired"
        rewritten.append(json.dumps(row, sort_keys=True))
    library.path.write_text("\n".join(rewritten) + "\n", encoding="utf-8")

    # Any ordinary mutation now erases the quarantined row and frees its number.
    library.revise(sop.sop_id, purpose="a third, entirely different procedure")

    resolved = library.get(sop.sop_id, pinned)
    assert resolved is not None, "the pinned version became unresolvable"
    assert resolved.purpose == "the v2 procedure: run the smoke tests first", (
        "the pinned version now resolves to text the instance never ran against"
    )


@pytest.mark.xfail(
    strict=True,
    reason="instantiate() delegates to queue.create(), which returns the pre-existing "
    "item on a natural-key collision without signalling that it did",
)
def test_instantiate_never_silently_returns_an_item_pinned_to_another_version(library, tmp_path):
    """Asking for a v2 instance can hand back a v1 instance, reported as success.

    `instantiate` builds `metadata["sop_ref"]` for the version it resolved, then
    passes it to `queue.create`. On a natural-key collision `create` discards
    that metadata entirely and returns the EXISTING item — which carries the pin
    it was created under. The caller receives a work item whose `sop_ref` names
    a version it did not ask for, with no exception, no refusal code and no
    remediation. `outcomes_by_version` then credits the run to the old version.

    `create` does set `existing.metadata["natural_key_conflict"] = True`, but on
    the returned object only — it is never written to the store, so nothing
    downstream can detect the substitution after the fact.

    Either resolution satisfies this test: refuse the instantiate when the
    resolved version does not match the existing item's pin, or return an item
    that actually carries the requested pin. What must not happen is the current
    silent mismatch. This is the "never a submission that returns success and
    produces nothing" rule in `errors.py`, applied to a submission that returns
    success and produces something ELSE.

    Why the existing tests miss it: `test_instantiating_creates_a_work_item_that_pins_the_version`
    instantiates once, into an empty queue, with no natural key — so the
    collision branch is never taken. `test_a_duplicate_natural_key_returns_the_existing_item`
    covers the collision but goes through `queue.create` directly, where there
    is no pin to be wrong about. The defect only appears where the two meet, and
    no test crosses them.
    """
    queue = Queue(tmp_path / "work.jsonl")
    sop = library.create("Nightly reconciliation", purpose="the v1 procedure")
    library.activate(sop.sop_id, 1)

    first = library.instantiate(sop.sop_id, queue, natural_key="nightly-2026-08-28")
    assert first.metadata["sop_ref"]["version"] == 1

    library.revise(sop.sop_id, purpose="the v2 procedure")
    library.activate(sop.sop_id, 2)

    second = library.instantiate(sop.sop_id, queue, natural_key="nightly-2026-08-28")

    assert second.metadata["sop_ref"]["version"] == 2, (
        "instantiate reported success but returned an item pinned to another version"
    )


# --------------------------------------------------------------------------- #
# Pointers, never copies — the invariant the design states in bold
# --------------------------------------------------------------------------- #


@pytest.mark.xfail(
    strict=True,
    reason="urllib's HTTPRedirectHandler rebuilds the request with no method=, so "
    "get_method() returns GET and a redirected HEAD transfers the body",
)
def test_an_https_pointer_is_never_resolved_by_a_body_transferring_get():
    """A single redirect downgrades the HEAD to a GET and pulls the whole body.

    `resolve_https` issues `Request(uri, method="HEAD")` and says so in bold:
    "HEAD rather than GET is the whole point: it returns the validator without
    transferring the body, so the 'never copy' invariant holds even for a
    resolver pointed at a large document."

    CPython's `urllib.request.HTTPRedirectHandler.redirect_request` constructs
    the follow-up as `Request(newurl, headers=..., origin_req_host=...,
    unverifiable=True)` — with no `method=` argument. `Request.get_method()`
    then returns `GET`. Any 301/302/303/307/308 therefore turns the
    metadata-only probe into a full download.

    This matters because redirects are the normal case for exactly the
    documents this resolver targets: hosted document stores, wikis, and
    anything behind a CDN or a canonical-URL rewrite.

    The server here records the method of every request it receives, so the
    assertion is on observed protocol behaviour rather than on the client's
    intent. Hermetic: loopback only, on an ephemeral port.

    Why the existing test misses it: `test_an_https_pointer_is_resolved_by_HEAD_so_no_body_is_fetched`
    monkeypatches `urllib.request.urlopen` with a `fake_urlopen` that returns a
    canned response, then asserts `request.get_method() == "HEAD"`. The fake
    replaces the entire opener chain — and the redirect handler, which is the
    component that performs the downgrade, lives inside that chain. The test
    asserts the method of the FIRST request, which is genuinely HEAD, and there
    is no second request for it to observe. It cannot fail for this bug.
    """
    canary = b"CANARY-body-that-should-never-be-transferred " * 32
    received: list[tuple[str, str]] = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def log_message(self, *args):  # keep the suite's output clean
            pass

        def do_HEAD(self):
            received.append(("HEAD", self.path))
            if self.path == "/moved":
                self.send_response(302)
                self.send_header("Location", "/final")
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("ETag", '"abc123"')
            self.end_headers()

        def do_GET(self):
            received.append(("GET", self.path))
            self.send_response(200)
            self.send_header("Content-Length", str(len(canary)))
            self.end_headers()
            self.wfile.write(canary)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        port = server.server_address[1]
        try:
            snapshots.resolve_https(f"http://127.0.0.1:{port}/moved")
        except snapshots.ResolverError:
            # The redirected GET carries no ETag, so resolution fails. Whether
            # it resolves is not the point; what the wire saw is.
            pass
    finally:
        server.shutdown()
        server.server_close()

    assert [method for method, _ in received] == ["HEAD", "HEAD"], (
        f"resolution transferred a body instead of reading a validator: {received}"
    )


# --------------------------------------------------------------------------- #
# Identity — what the token says, and what the payload is allowed to say
# --------------------------------------------------------------------------- #


@pytest.mark.xfail(
    strict=True,
    reason="the conflict record carries withHolder but not holderAttested, so a "
    "conflict raised by an unverified holder claim is indistinguishable from a real one",
)
def test_a_conflict_from_an_unverified_holder_claim_is_distinguishable(client):
    """A client can make a third party appear to be holding scope they never claimed.

    `leases.claim` takes `holder` straight from the request body:
    `claimed_holder = (holder or actor).strip() or actor`. The lease is flagged
    `holderAttested` when the two differ, which `leases.py:18-21` says makes it
    "a weaker claim, not rejected as a lie" — and `docs/architecture.md` says an
    unattested holder claim "cannot block anything".

    It blocks the only thing stage 1 has. `find_conflicts` treats attested and
    verified leases identically, and the conflict record it returns carries
    `withHolder`, `overlaps` and `theirIntent` — but NOT `holderAttested`. Every
    subsequent claimant on that prefix is told they are colliding with a named
    colleague, with nothing to indicate the claim was filed by somebody else.
    `MAX_TTL_S` allows fourteen days of it, and `release` refuses the filer,
    because the recorded holder is the impersonated party.

    The reach is the TIER-3 SESSION HOOK, not the shared repo file. Be precise
    about this, because the wrong module wastes a fixer's time:
      * `inject.py` renders the shared `CLAUDE.md`/`AGENTS.md` block from
        `live_leases` directly, and it DOES surface the flag — `inject.py:258`
        appends "(attested, unverified)". That path is fine.
      * `hook.py:115` calls `leases.conflicts_for`, whose records come from
        `scope.find_conflicts` and carry only `withHolder`, `overlaps` and
        `theirIntent`. That output goes to `inject.render_session_block` and
        into the harness's session context with no flag at all.
    So the fix belongs in the conflict record itself (`scope.find_conflicts`),
    which is where both the API response and the session hook read from —
    not in either renderer.

    Either resolution satisfies this test: propagate the flag into the conflict
    so a consumer can weigh it, or do not fire third-party conflicts from an
    unattested lease at all. What must not happen is the current state, where
    the two are identical on the wire. `auth.py:15-19` names this scenario
    exactly as the thing the design forecloses.

    Why the existing test misses it: `test_claiming_in_someone_elses_name_is_recorded_as_attested_not_trusted`
    asserts `holderAttested is True` on the CLAIMANT's own receipt. That flag is
    set correctly. The test never has a third party claim an intersecting prefix,
    so it never looks at the conflict record — which is the surface where the
    flag is missing and the only surface anyone else reads.
    """
    filed_in_another_name = post(
        client,
        "/scope-claims",
        "kofi",
        {
            "repo": "acme/web-platform",
            "prefixes": ["src/billing/invoices"],
            "intent": "implement",
            "holder": "dana",
            "ttlSeconds": 14 * 24 * 3600,
        },
    ).json()
    assert filed_in_another_name["holderAttested"] is True, "precondition: recorded as attested"

    third_party = post(
        client,
        "/scope-claims",
        "operator",
        {"repo": "acme/web-platform", "prefixes": ["src/billing/invoices"], "intent": "implement"},
    ).json()

    conflicts = third_party["conflicts"]
    assert conflicts, "precondition: the attested lease fires a conflict at a third party"
    assert "holderAttested" in conflicts[0], (
        "a conflict sourced from an unverified holder claim is reported as though "
        f"the named holder filed it: {conflicts[0]}"
    )


@pytest.mark.xfail(
    strict=True,
    reason="the bad-signature branch appends the signing-string spec to its "
    "remediation, so the two refusals differ in a field to_dict() ships to the client",
)
def test_an_unknown_actor_and_a_bad_signature_are_indistinguishable_refusals():
    """The two refusals differ in `remediation`, which makes the endpoint an identity oracle.

    `auth.py:143-145` states the intent: "Deliberately the same message as a bad
    signature: distinguishing 'no such actor' from 'wrong secret' turns this
    endpoint into an identity oracle for anyone who can reach it."

    The `message` fields are identical. The `remediation` fields are not — the
    bad-signature branch appends a sentence describing the signing string. Both
    fields are returned to the client by `Refusal.to_dict()` and rendered into
    the HTTP body, so one request per guess enumerates which actor names are
    registered.

    There is a second, weaker channel in the same code: the unknown-actor branch
    returns before any HMAC is computed, while the known-actor branch runs
    HMAC-SHA256 and `compare_digest` first. That is a timing difference, and it
    is not what this test measures — the response body is sufficient on its own.

    Comparing the whole `to_dict()` is deliberate. Any future field added to a
    refusal is covered without anyone remembering to extend this test.

    Why the existing test misses it: `test_an_unknown_actor_and_a_bad_signature_are_indistinguishable`
    compares `exc.value.message` and nothing else. `message` is the half that is
    equal; `remediation` is the half that differs and the half that leaks. The
    test asserts precisely the field that cannot fail.
    """
    ts = str(int(time.time()))

    def refusal_for(actor: str) -> dict:
        with pytest.raises(Unauthenticated) as exc:
            auth.authenticate(
                {
                    "x-agentco-actor": actor,
                    "x-agentco-timestamp": ts,
                    "x-agentco-signature": "00" * 32,
                },
                "POST",
                "/scope-claims",
                b"{}",
                keys=KEYS,
            )
        return exc.value.to_dict()

    unknown_actor = refusal_for("nobody-is-registered-here")
    wrong_secret = refusal_for("dana")

    assert unknown_actor == wrong_secret, (
        "the refusals are distinguishable, so actor existence is a one-request oracle"
    )


# --------------------------------------------------------------------------- #
# leakguard — a guard that reports "clean" is worse than no guard
# --------------------------------------------------------------------------- #
#
# The three below share a failure mode: the tool exits 0 and prints "clean"
# while the thing it exists to catch goes straight past it. That is worse than
# having no guard, because it converts an absent check into a false assurance
# — and the whole publishability argument in CONTRIBUTING.md rests on this
# tool actually running.

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "leakguard"))

import leakguard  # noqa: E402


def leakguard_rules_hit(text: str) -> set[str]:
    return {f.rule for f in leakguard.scan_text(text, "f.md", leakguard.BASE_RULES)}


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    )


git_available = shutil.which("git") is not None


@pytest.mark.skipif(not git_available, reason="git is required to stage anything")
@pytest.mark.xfail(
    strict=True,
    reason="staged_files() lists names from the index but scan_paths() reads each "
    "path from the working tree, so the bytes being committed are never examined",
)
def test_staged_mode_scans_the_content_that_is_about_to_be_committed(tmp_path):
    """`--staged` checks the working tree, so a scrubbed worktree hides a staged secret.

    `staged_files` asks git for names only —
    `git diff --cached --name-only --diff-filter=ACMR` — and hands back
    `root / line`. `scan_paths` then calls `path.read_text()`, which reads the
    WORKING TREE copy. The index, which is what `git commit` will actually
    record, is never read.

    So the pre-commit hook approves a commit containing a credential whenever
    the worktree copy differs from the staged copy. That is not an exotic
    sequence: stage a file, then keep editing it — which is what anyone does
    when they stage a fix and then clean up around it. The hook reports
    "clean (1 file(s) scanned)" and exits 0.

    The same read is why a file staged and then DELETED from the worktree is
    skipped silently rather than reported: `read_text` raises `FileNotFoundError`,
    `scan_paths` catches `OSError`, and the loop continues.

    Worth fixing together with the sibling fail-open in the same function: when
    `git diff` exits non-zero, `staged_files` returns `[]`, so `main` finds no
    findings and exits 0. A git failure currently reads as a pass.

    Why the existing tests miss it: `test_leakguard.py` has no `--staged` test
    at all. Every test there either calls `scan_text` on a literal string or
    `main(["--root", ...])` in whole-tree mode, where worktree and index are
    the same bytes by construction. The one mode the pre-commit hook actually
    runs in is the one mode never exercised.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")

    secret = repo / "config.py"
    secret.write_text('TOKEN = "ghp_' + "a" * 30 + '"\n', encoding="utf-8")
    _git(repo, "add", "config.py")

    # The worktree is cleaned up afterwards; the index still holds the token.
    secret.write_text('TOKEN = os.environ["TOKEN"]\n', encoding="utf-8")
    staged = _git(repo, "show", ":config.py").stdout
    assert "ghp_" in staged, "precondition: the credential is staged for commit"

    exit_code = leakguard.main(["--root", str(repo), "--staged"])

    assert exit_code == 1, "the hook approved a commit that records a live credential"


@pytest.mark.xfail(
    strict=True,
    reason="the (?<!0\\.0\\.0\\.0) lookbehind sits after \\b and matches any address "
    "whose last seven characters are 0.0.0.0, so the whole N0.0.0.0 family escapes",
)
def test_a_private_network_address_is_flagged_even_when_it_ends_in_zeros():
    """`10.0.0.0/8` — the most common private range there is — is invisible to the guard.

    The rule is
    `\\b(?:\\d{1,3}\\.){3}\\d{1,3}\\b(?<!0\\.0\\.0\\.0)(?<!127\\.0\\.0\\.1)`.
    Both lookbehinds are placed AFTER the closing `\\b`, so they are evaluated
    at the position following the whole match and test the preceding
    characters of whatever was matched — not whether the match IS the exempt
    address. Any address whose final seven characters are `0.0.0.0` therefore
    satisfies the negative lookbehind and is dropped.

    That is the entire `N0.0.0.0` family: `10.0.0.0`, `20.0.0.0` … `90.0.0.0`,
    `100.0.0.0`, `250.0.0.0`. It is not a broad rule failure — host addresses
    and non-zero networks are still caught, as the second block below pins.
    What escapes is precisely the NETWORK addresses, which are the ones most
    likely to appear in a committed config, a firewall rule or a subnet
    comment.

    The loopback assertions are kept in the same test on purpose. They are what
    stops the fix being "drop the lookbehinds and flag everything", which would
    fire on every default binding in the repo and train people to suppress the
    rule — the failure mode `test_loopback_is_not_flagged_as_a_private_host`
    exists to prevent. A correct fix anchors the exemption to the match itself
    rather than to the text behind it.

    Why the existing test misses it: `test_loopback_is_not_flagged_as_a_private_host`
    checks `127.0.0.1` and `0.0.0.0` — the two addresses the lookbehinds were
    written for, both of which work. `test_an_internal_hostname_is_caught` uses
    a hostname, not an address. No test asserts that a non-exempt address IS
    caught, so the over-broad exemption has nothing to fail against.
    """
    for address in ("10.0.0.0", "100.0.0.0", "20.0.0.0", "250.0.0.0"):
        assert "private-host" in leakguard_rules_hit(f"upstream {address}"), (
            f"{address} is a private network address and was not flagged"
        )

    # Addresses that already work must keep working, so the fix cannot narrow
    # the rule instead of correcting the exemption.
    assert "private-host" in leakguard_rules_hit("upstream 10.0.0.1")  # leakguard: allow
    assert "private-host" in leakguard_rules_hit("upstream 172.16.0.0")  # leakguard: allow

    # The exemptions must survive the fix — otherwise every default binding in
    # the repo becomes a finding, and the rule gets suppressed rather than used.
    # (`test_loopback_is_not_flagged_as_a_private_host` in test_leakguard.py is
    # the backstop that goes RED if someone "fixes" this by dropping the
    # lookbehinds wholesale; these two keep that visible from here as well.)
    assert "private-host" not in leakguard_rules_hit("uvicorn --host 127.0.0.1")
    assert "private-host" not in leakguard_rules_hit("bind 0.0.0.0:8787")


@pytest.mark.xfail(
    strict=True,
    reason='Path(".env.example").suffix is ".example", so the ".env.example" entry in '
    "DEFAULT_INCLUDE_SUFFIXES can never match and the file is never opened",
)
def test_an_env_example_file_is_scanned_for_leaks(tmp_path):
    """The one filename most likely to hold a credential is excluded by a typo in the include set.

    `iter_files` filters on `path.suffix.lower() not in config.include_suffixes`.
    `Path.suffix` returns only the FINAL extension, so `.env.example` yields
    `".example"` — while `DEFAULT_INCLUDE_SUFFIXES` lists the full
    `".env.example"`. The entry is unreachable: no path's `suffix` can ever
    equal it. The file is silently skipped and the tool reports "clean".

    An `.env.example` is precisely where a real value gets pasted by accident
    instead of a placeholder, which is why someone thought to list it.

    The same `suffix`-based filter also skips every extensionless file —
    `Dockerfile`, `Makefile`, and `tools/leakguard/pre-commit`, the hook script
    shipped in this repo. Verified: a `ghp_` token in either `.env.example` or
    `Dockerfile` passes the whole-tree scan.

    Why the existing tests miss it: every fixture in `test_leakguard.py` that
    goes through `main`/`iter_files` is named `*.md` or `*.py`. Nothing tests
    the include-set membership itself, so an entry that can never match looks
    identical to one that works.
    """
    (tmp_path / ".env.example").write_text(
        "API_TOKEN=ghp_" + "a" * 30 + "\n", encoding="utf-8"
    )

    assert leakguard.main(["--root", str(tmp_path)]) == 1, (
        "a credential in .env.example passed the scan; the include-set entry is unreachable"
    )
