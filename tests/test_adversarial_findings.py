"""Defects found by adversarial review, each as a test of the property that SHOULD hold.

Every test here began life FAILING against the code, each marked
`xfail(strict=True)`. That marker does two things and both are deliberate:

  * the suite stays green while the defect stands, so this file can be merged
    without blocking anything;
  * the suite goes RED the moment a defect is fixed and the marker is not
    removed. A silently-passing xfail is how a fixed bug stops being tracked,
    which is the same silent-success failure mode the codebase refuses
    everywhere else. Fix the defect, delete the marker, in one change.

The names state the property that should hold, never the bug. If you are
reading one of these because it started failing as an XPASS: that is the
system working. Remove the marker.

**Some now pass.** A test WITHOUT a marker is a defect that has been fixed,
kept as a regression test. Those are the valuable ones — they are the only
tests in the suite known to have caught a real bug in this code. Two rules for
them, learned from doing it wrong here:

  * Assert the PROPERTY, not the route the original defect took. A repro that
    calls the API the fix now refuses will raise during setup and go on
    xfailing for a reason that has nothing to do with the property.
  * A newly-passing test must be shown to FAIL against the pre-fix code before
    the marker comes off, or it may be passing vacuously. Checking out the
    previous revision of the one module and re-running is enough.

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
from agentco.sop import SopError, SopLibrary
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
def conn_tmp(tmp_path):
    return db.connect(tmp_path / "registry.sqlite3")


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


def test_a_pinned_sop_version_never_resolves_to_different_text(library, tmp_path):
    """A pin must never point at text the instance did not run against. FIXED in a71650a.

    Marker removed because the property now holds. Kept as a regression test,
    and rewritten so it asserts the PROPERTY rather than the route the original
    defect took — the previous version called `revise()` and read the result,
    which now raises during setup and would have left this xfailing for the
    wrong reason.

    THE ORIGINAL DEFECT. Two mechanisms composed. `_read_all` quarantined any
    row it could not parse, and `_write_all` rewrote the file from the survivors
    only — so the quarantined version was destroyed by the next mutation.
    `revise()` computes the next version as `max(surviving versions) + 1`, which
    REISSUED the number that had just been destroyed, to completely different
    text. The instance's `sop_ref` never changed. Version 2 simply began saying
    something else, and `outcomes_by_version` would attribute the instance's
    outcome to a procedure it never ran — "every number computed from it would
    be fiction" (`sop.py:22-25`).

    WHY THIS ASSERTS A DISJUNCTION. Preserving the quarantined bytes was
    necessary but NOT sufficient: with the row kept-but-unparseable, `revise()`
    still could not see it and still reissued the number. The remedy shipped was
    to refuse `revise()` while any line is quarantined — a caller who cannot see
    the whole history cannot safely choose the next number. So there are two
    acceptable end states and this accepts either:
      * the pin still resolves to its original text, or
      * the pin does not resolve at all, AND the attempt to reissue its number
        was refused.
    What is NOT acceptable, and what the original code did, is the third: the
    pin resolves, silently, to something else.

    The final assertion is the one that makes "unresolvable" tolerable rather
    than a second bug. An unreadable row whose bytes are gone is data loss; an
    unreadable row preserved verbatim is a repair job. Without this check the
    disjunction above could be satisfied by deleting the row outright.

    Why the existing tests missed it: `test_a_superseded_version_stays_resolvable_forever`
    and `test_a_revision_does_not_reach_back_into_a_running_instance` both
    operate on a store where every row round-trips cleanly. No test in
    `test_sop.py` writes a row the reader cannot parse, so the quarantine branch
    was never entered and the version-number reissue it enabled was unreachable.
    """
    original_v2 = "the v2 procedure: run the smoke tests first"
    sop = library.create("Deploy the service", purpose="the original v1 procedure")
    library.revise(sop.sop_id, purpose=original_v2)
    library.activate(sop.sop_id, 2)

    queue = Queue(tmp_path / "work.jsonl")
    item = library.instantiate(sop.sop_id, queue, title="deploy run")
    pinned = item.metadata["sop_ref"]["version"]
    assert library.get(sop.sop_id, pinned).purpose == original_v2

    # A newer writer stores a status this version does not know about, so the
    # pinned row can no longer be parsed by this reader.
    rewritten = []
    for line in library.path.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        if row["version"] == pinned:
            row["status"] = "retired"
        rewritten.append(json.dumps(row, sort_keys=True))
    library.path.write_text("\n".join(rewritten) + "\n", encoding="utf-8")

    # Attempt the re-point. Refusing is one of the two honest outcomes.
    try:
        library.revise(sop.sop_id, purpose="a third, entirely different procedure")
    except SopError:
        pass

    # Ordinary unrelated activity must not open a second route to the same
    # re-point: both of these also rewrite the whole file.
    library.create("An unrelated procedure", purpose="nothing to do with the above")
    library.activate(sop.sop_id, 1)

    resolved = library.get(sop.sop_id, pinned)
    assert resolved is None or resolved.purpose == original_v2, (
        f"the pin resolves to text the instance never ran against: {resolved.purpose!r}"
    )
    assert original_v2.encode("utf-8") in library.path.read_bytes(), (
        "the pinned version's text was destroyed; unresolvable is only acceptable "
        "while the bytes are preserved and the row can be repaired"
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


# --------------------------------------------------------------------------- #
# The scope model — a claim that is invisible is worse than no claim
# --------------------------------------------------------------------------- #
#
# `scope.py` opens by arguing that a lease registry's entire value IS
# composition: "does my claim intersect yours?" The segment-wise intersection
# is genuinely correct — `src/budget` and `src/budgeting` do not collide, and
# `test_intersection_is_segmentwise_not_string_prefix` proves it.
#
# What follows is the other half of the property, which nothing tests: two
# claims that DO name the same directory can be made not to intersect. Each
# costs one keystroke or one paste, and in every case below the two spellings
# render identically or address the same directory on a real filesystem. For a
# tool whose one product is answering "is anyone else in here?", a
# visually-identical non-intersecting claim is an evasion primitive — and,
# more often, an honest accident that silently disables the feature.
#
# EVERY assertion below accepts either honest fix: canonicalise the two forms
# so they intersect, OR refuse the non-canonical form with a remediation. The
# helpers encode that, so none of these tests dictates a remedy.
#
# Each test also PINS the behaviour a fix must not break. Those pins are
# verified to hold today, so a correct fix turns the whole test green rather
# than moving the failure down a line.


def prefixes_collide_or_are_refused(raw_a: str, raw_b: str) -> bool:
    """Does the registry treat these two spellings as one directory, by any honest route?

    Two remedies are legitimate and this accepts both:
      * canonicalise — fold the difference away so the prefixes intersect;
      * refuse — reject the non-canonical spelling with a remediation, so the
        caller re-claims in the form the registry can compare.
    What is NOT acceptable is the current third outcome: both accepted, and
    silently disjoint.
    """
    try:
        validate_prefix(raw_a)
        validate_prefix(raw_b)
    except Refusal:
        return True
    return prefixes_overlap(raw_a, raw_b)


def scopes_collide_or_are_refused(repo_a: str, repo_b: str, prefixes: list[str]) -> bool:
    """The same question one level up, for two spellings of one repo name."""
    try:
        first = Scope.parse(repo_a, prefixes)
        second = Scope.parse(repo_b, prefixes)
    except Refusal:
        return True
    return bool(scopes_intersect(first, second))


def assert_unrelated_scopes_stay_disjoint() -> None:
    """The pins every fix in this section must preserve.

    Written as one helper because all six intersection tests must not break the
    same three things: the segment-wise rule, genuinely different repos, and
    the parent/child containment the whole model rests on. A fix that makes the
    evasions collide by loosening the comparison — substring matching, dropping
    all punctuation, ignoring the repo — would satisfy the primary assertion
    and break these.
    """
    assert not prefixes_overlap("src/budget", "src/budgeting"), "segment-wise rule lost"
    assert not prefixes_overlap("src/billing", "src/shipping"), "unrelated dirs now collide"
    assert prefixes_overlap("src/billing", "src/billing/invoices"), "containment lost"
    assert not scopes_intersect(
        Scope.parse("acme/web-platform", ["src/billing/invoices"]),
        Scope.parse("acme/mobile-app", ["src/billing/invoices"]),
    ), "different repos now collide"


@pytest.mark.xfail(
    strict=True,
    reason="scopes_intersect compares repo with a plain !=, so one changed letter "
    "makes a lease invisible to every other claim in the registry",
)
def test_two_spellings_of_one_repo_name_do_not_hide_a_lease():
    """One capital letter in the repo name hides a lease from the ENTIRE registry.

    `scopes_intersect` short-circuits on `if a.repo != b.repo: return ()`, and
    `validate_repo` only strips whitespace. GitHub and Azure DevOps both treat
    repository names case-insensitively — `acme/Web-Platform` and
    `acme/web-platform` are one repository, and a URL with either spelling
    resolves to it.

    This is the worst of the evasions in this section because it is not scoped
    to one directory. A lease filed under the other casing intersects NOTHING:
    it fires no conflicts, appears in no other claimant's response, and its
    holder is told they are alone. Both parties then believe the registry
    checked, which is worse than either of them knowing it had not.

    It is also the likeliest to happen by accident. The repo string comes from
    whatever the caller's tooling reports — a git remote URL, a CI variable, a
    hand-typed `.mcp.json` entry — and those disagree about case constantly.

    Why the existing test misses it: `test_scopes_in_different_repos_never_intersect`
    asserts that `repo/one` and `repo/two` do not intersect. That is the
    property holding in the direction where it works. Nothing asserts the
    converse — that two names for the SAME repo do intersect — so a comparison
    that is too strict has nothing to fail against.
    """
    assert scopes_collide_or_are_refused(
        "acme/web-platform", "acme/Web-Platform", ["src/billing/invoices"]
    ), "a lease under a different capitalisation of the same repo is invisible registry-wide"

    assert_unrelated_scopes_stay_disjoint()


@pytest.mark.xfail(
    strict=True,
    reason="normalize_prefix does not fold case, so two spellings of one directory "
    "on a case-insensitive filesystem produce two non-intersecting claims",
)
def test_two_spellings_of_one_directory_do_not_hide_a_claim():
    """`src/Budget` and `src/budget` are the same directory and do not intersect.

    `normalize_prefix` strips slashes and resolves dot segments but never
    touches case, so the two spellings become two distinct segment tuples.
    macOS (APFS, case-insensitive by default) and Windows (NTFS) both resolve
    them to one directory — and `scope.py:50-55` explicitly justifies using
    `posixpath` over `os.path` so the module "must behave identically on the
    Windows VM", which makes Windows a named target rather than a hypothetical.

    Two developers editing the same directory therefore get no conflict, which
    is the exact outcome the module exists to prevent.

    Note this one has a genuine trade-off and the test does not pretend
    otherwise: on a case-SENSITIVE filesystem (ext4) the two really are
    different directories, so folding case would produce a false conflict
    there. That is why the assertion accepts a refusal as readily as a fold —
    "refuse the ambiguous spelling and make the caller pick" is a legitimate
    answer, and arguably the better one.

    Why the existing test misses it: `test_two_segments_is_accepted_and_canonicalised`
    checks that `./src/budget/` and `src\\budget` canonicalise to `src/budget`.
    Both inputs differ from the target only in punctuation, which
    `normalize_prefix` does handle. No test varies case, so the one dimension
    it does not normalise is the one dimension never exercised.
    """
    assert prefixes_collide_or_are_refused("src/Budget", "src/budget"), (
        "two spellings of one directory produced two disjoint claims"
    )

    assert_unrelated_scopes_stay_disjoint()


@pytest.mark.xfail(
    strict=True,
    reason="str.strip() removes NBSP but not U+200B or U+FEFF, so a zero-width "
    "character survives normalisation and creates a visually identical disjoint claim",
)
def test_an_invisible_character_cannot_make_a_claim_disjoint():
    """A zero-width space makes a claim that LOOKS identical intersect nothing.

    `normalize_prefix` calls `.strip()`, which removes Unicode whitespace, and
    that is where the inconsistency shows. A trailing NBSP (U+00A0) IS
    whitespace, so `src/budget\\u00a0` collapses correctly to `src/budget` —
    asserted below as evidence. A zero-width space (U+200B) and a byte-order
    mark (U+FEFF) are NOT whitespace by Python's definition, so they survive
    and produce a distinct segment tuple.

    That NBSP is handled and U+200B is not is the evidence this is an accident
    rather than a decision: nothing would justify normalising one invisible
    trailing character and preserving another. There is no design note either
    way, which is what an unconsidered case looks like.

    Both spellings render identically in a terminal, a code review, a web UI
    and the registry's own conflict output. Someone reading the lease list
    cannot see the difference, so the disagreement is undebuggable from the
    outside — and a BOM in particular arrives on its own, pasted in from a file
    written by an editor that emits one.

    Why the existing test misses it: no test in `test_registry.py` passes a
    non-ASCII prefix at all. Every scope fixture is plain ASCII, so the entire
    class — invisible characters, normalisation forms, homoglyphs — is outside
    what the suite can observe.
    """
    assert prefixes_collide_or_are_refused("src/budget", "src/budget​"), (
        "a trailing zero-width space produced a visually identical disjoint claim"
    )
    assert prefixes_collide_or_are_refused("src/budget", "﻿src/budget"), (
        "a leading byte-order mark produced a visually identical disjoint claim"
    )

    # Evidence, and a pin: the whitespace case already collapses. A fix should
    # extend that handling, not replace it.
    assert prefixes_overlap("src/budget", "src/budget "), "NBSP handling regressed"
    assert_unrelated_scopes_stay_disjoint()


@pytest.mark.xfail(
    strict=True,
    reason="normalize_prefix does not apply a Unicode normalisation form, so NFC and "
    "NFD spellings of one path are two distinct segment tuples",
)
def test_the_same_path_intersects_regardless_of_unicode_normalisation_form():
    """The same directory name from two machines does not intersect itself.

    `normalize_prefix` never calls `unicodedata.normalize`, so a path
    containing any accented character has two byte-level spellings that both
    survive: composed (NFC — one code point) and decomposed (NFD — base letter
    plus combining accent).

    This is not theoretical for this project. macOS stores filenames in a
    decomposed form, so a path read off a mac filesystem arrives NFD, while the
    same path typed on Linux or pasted from a browser arrives NFC. Two
    colleagues claiming the identical directory from different machines
    therefore never see each other — and the strings are indistinguishable in
    every UI, so neither can tell why.

    A non-ASCII directory name is unremarkable in a codebase with any
    non-English content, which the project's own context makes likely.

    Why the existing test misses it: same as the invisible-character case — no
    scope fixture anywhere in the suite contains a non-ASCII character, so
    there is no input from which the two forms could diverge.
    """
    composed = unicodedata.normalize("NFC", "src/café")
    decomposed = unicodedata.normalize("NFD", "src/café")
    assert composed != decomposed, "precondition: the two forms differ byte-for-byte"

    assert prefixes_collide_or_are_refused(composed, decomposed), (
        "one directory name in two Unicode forms produced two disjoint claims"
    )

    assert_unrelated_scopes_stay_disjoint()


@pytest.mark.xfail(
    strict=True,
    reason="a trailing dot survives normalize_prefix, but Windows strips it, so the "
    "two spellings address one directory and produce two disjoint claims",
)
def test_a_trailing_dot_does_not_make_a_claim_disjoint():
    """`src/budget.` and `src/budget` are one directory on Windows and do not intersect.

    `posixpath.normpath` removes `.` only as a whole segment. A dot at the END
    of a segment name is preserved, so `src/budget.` stays a distinct tuple.
    Windows strips trailing dots from path components — the two names cannot
    coexist there and resolve to the same directory. `scope.py:50-55` names the
    Windows VM as a target platform, so this is a supported-platform
    disagreement rather than a curiosity.

    Milder than the others in likelihood, but the same shape: two spellings,
    one directory, no conflict.

    The pin below is the one that matters most for this fix. Stripping dots too
    eagerly would collapse `src/v1.2/grid` into something that matches
    `src/v1/grid`, inventing a conflict between two genuinely different
    directories — a false positive in a system whose stated failure mode is
    becoming noise.

    Why the existing test misses it: `test_two_segments_is_accepted_and_canonicalised`
    covers `./` and a trailing `/`, the two punctuation forms `normpath` and
    `strip` already handle. A trailing dot inside the final segment is handled
    by neither, and nothing tests it.
    """
    assert prefixes_collide_or_are_refused("src/budget", "src/budget."), (
        "a trailing dot produced a claim disjoint from the directory it names"
    )

    # A dot INSIDE a name is meaningful and must survive any fix.
    assert not prefixes_overlap("src/v1.2/grid", "src/v1/grid"), (
        "interior dots were stripped; two different directories now collide"
    )
    assert_unrelated_scopes_stay_disjoint()


@pytest.mark.xfail(
    strict=True,
    reason="only Scope.parse validates; the dataclass constructor accepts anything, "
    "and the docstring claims it is validated on construction",
)
def test_constructing_a_scope_directly_validates_it():
    """`Scope`'s docstring claims validation it does not perform.

    The class docstring reads: "`(repo, path-prefix set)` — the whole scope
    model, validated on construction." Validation lives entirely in the
    `parse()` classmethod. The generated dataclass `__init__` accepts an empty
    repo, a one-segment prefix that `scope_too_broad` exists to refuse, an
    empty-string prefix, and a `../../etc` escape that
    `test_a_prefix_escaping_the_repo_is_refused_not_normalised_away` proves is
    refused through the other door.

    This is the "comment claims a property the code does not have" class, and
    it has a live caller: `leases.live_leases` reconstructs
    `Scope(l["repo"], tuple(l["prefixes"]))` on every read of the lease table,
    and `leases.conflicts_for` does the same. Today those inputs are safe
    because they were validated on the way in — so the exposure is a
    correctness argument that depends on a claim the type does not enforce, and
    on nobody adding a second writer to that table.

    The pin is essential here rather than decorative: `live_leases` runs this
    constructor on every already-canonical stored prefix, so validation added
    in `__post_init__` must accept canonical input unchanged. A fix that
    re-normalises or rejects stored values would break every conflict lookup in
    the registry.

    Why the existing test misses it: every scope test constructs through
    `Scope.parse`, which is the documented entry point and does validate. The
    constructor the docstring actually describes is never called directly by a
    test, so its claim is never checked.
    """
    with pytest.raises(Refusal):
        Scope(repo="", prefixes=("src", "../../etc", ""))

    # `live_leases` depends on this: canonical input must construct unchanged.
    reconstructed = Scope(repo="acme/web-platform", prefixes=("src/billing/invoices",))
    assert reconstructed.prefixes == ("src/billing/invoices",)
    assert reconstructed.repo == "acme/web-platform"


@pytest.mark.xfail(
    strict=True,
    reason="Scope.parse iterates a str character-by-character, so the refusal names a "
    "single-character prefix the caller never sent",
)
def test_a_string_of_prefixes_is_refused_without_inventing_a_prefix():
    """Passing a string for `prefixes` produces a refusal that quotes a prefix nobody sent.

    `Scope.parse` does `for raw in prefixes`, and a `str` is iterable. A caller
    who sends `"prefixes": "src/budget/grid"` — a JSON string where an array
    was wanted, which is the single most common shape error against any API —
    has their input iterated character by character. The first character fails
    the depth check and the caller is told:

        scope_too_broad: path prefix 's' names 1 segment(s) below the repo root

    The refusal is well-formed, carries a code and a remediation, and is about
    a prefix that does not exist. `errors.py` argues that the remediation is
    what stops a colleague concluding the tool is broken on their first
    refused POST; a remediation telling them to deepen `'s'` does the opposite,
    because nothing they can do to their actual input will address it.

    This is the silent-coercion shape: a type error normalised into a
    content error rather than refused as what it is.

    Any accurate refusal satisfies this test — a distinct code for the wrong
    type, or a message naming the string the caller actually sent. The
    assertion targets only the specific dishonesty of reporting a
    single-character prefix as though it were submitted.

    Why the existing test misses it: every scope fixture passes a real list.
    Nothing in the suite sends a wrong-typed `prefixes`, so the iteration is
    never observed doing something other than iterating prefixes.
    """
    with pytest.raises(Refusal) as exc:
        Scope.parse("acme/web-platform", "src/budget/grid")

    refusal = exc.value
    invented_a_prefix = refusal.code == "scope_too_broad" and "'s'" in refusal.message
    assert not invented_a_prefix, (
        f"the refusal describes a prefix the caller never sent: {refusal.message!r}"
    )

    # A genuinely too-shallow prefix must still get the refusal it is for.
    with pytest.raises(Refusal) as shallow:
        Scope.parse("acme/web-platform", ["src"])
    assert shallow.value.code == "scope_too_broad"


# --------------------------------------------------------------------------- #
# Resolution must never block the write
# --------------------------------------------------------------------------- #


def test_a_pointer_whose_target_has_moved_is_recorded_rather_than_refused(conn_tmp):
    """The two shipped resolvers block the write that the module says is never blocked.

    `snapshots.py:15-21` is unambiguous: "**Resolution never blocks the write.**
    A scheme with no registered resolver is still recorded, because the
    alternative — refusing the write — makes the most valuable endpoint
    unreachable until whatever the resolver needs is in place." And `:23-28`:
    "an unresolvable snapshot is recorded **loudly**, never silently."

    `resolve_git` (a rev that will not parse) and `resolve_file` (a path that
    does not exist) both raise `Refusal`. `resolve()` catches only
    `ResolverError`, so the `Refusal` travels straight out through `take()` and
    the snapshot is never written. Over HTTP that is a 422 and no row.

    The asymmetry is the tell, and it is backwards. Measured:
      * `widgetstore:` — a third-party resolver raising `ResolverError`
        → RECORDED, `externalResolution: unresolvable`, reason carried;
      * `unknownscheme:` — no resolver at all
        → RECORDED, `externalResolution: unresolvable`;
      * `git:` / `file:` — the two resolvers that ship in the box
        → REFUSED, `unresolvable_uri`, nothing written.
    A connector written by someone else gets the documented degradation. The
    built-ins do not.

    It also fails on exactly the artifacts most worth tracking. "The thing I
    baselined has moved or been deleted" is the single strongest reason to
    record a pointer — `ResolverError`'s own docstring calls it "a real signal
    about the artifact" — and it is the one case where recording is impossible.
    Worse, it is silent in the wrong direction: the caller is told their URI is
    bad, so they fix the URI rather than learning the artifact moved.

    Either fix works and this accepts both: raise `ResolverError` from the
    shipped resolvers for an unreadable target, or have `resolve()` convert a
    resolver's `Refusal` into an unresolved reason. Either way the receipt must
    carry the reason, because the whole argument for recording an unresolvable
    pointer is that it says so loudly.

    The pins below matter: a fix must not turn `take()` into a function that
    accepts anything. A URI with no scheme and a blank purpose are CALLER
    errors, not unreadable artifacts, and both must still be refused — that
    distinction is the entire point of `ResolverError` existing separately from
    `Refusal`.

    Why the existing test misses it: `test_a_resolver_that_cannot_read_its_target_degrades_rather_than_crashes`
    registers a fake `widgetstore` resolver that raises `ResolverError` and
    asserts it degrades. It does — that path is correct. The test proves the
    contract for a resolver that follows it, using a resolver written by the
    test itself, and never points a SHIPPED resolver at a missing target. The
    two implementations in the module that break the contract are the two the
    suite never exercises this way.
    """
    missing_file = snapshots.take(
        conn_tmp,
        actor="dana",
        artifact_uri="file:/no/such/file/anywhere",
        purpose="baseline for the redesign",
    )
    assert missing_file["state"] == "accepted"
    assert missing_file["freshness"]["externalResolution"] == "unresolvable"
    assert missing_file["freshness"]["reason"], "recorded, but not loudly — no reason given"

    unreadable_rev = snapshots.take(
        conn_tmp,
        actor="dana",
        artifact_uri="git:/no/such/repo#main",
        purpose="baseline for the redesign",
    )
    assert unreadable_rev["state"] == "accepted"
    assert unreadable_rev["freshness"]["externalResolution"] == "unresolvable"

    # Caller errors are NOT unreadable artifacts and must still be refused.
    with pytest.raises(Refusal) as no_scheme:
        snapshots.take(
            conn_tmp, actor="dana", artifact_uri="/absolute/path/no/scheme", purpose="x"
        )
    assert no_scheme.value.code == "bad_uri"

    with pytest.raises(Refusal) as no_purpose:
        snapshots.take(conn_tmp, actor="dana", artifact_uri="file:/tmp", purpose="  ")
    assert no_purpose.value.code == "purpose_required"
