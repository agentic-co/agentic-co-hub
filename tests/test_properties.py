"""Property-based tests for the hub's coordination invariants.

Most recorded defects in this system are protocol bugs (fences, epochs,
idempotency, races). Example tests pin each bug once found; these search for
the next one. Each property is stated as the claim the docs make, not as a
transcript of the code.

  1. Fencing   - only the current lease attempt, from its holder, changes an
                 item; at most one live lease; the attempt never decreases.
  2. Gates     - verify_failures climbs by one per failing verdict that lands, a
                 stale verify_attempt never lands, DONE is final.
  3. Replays   - an idempotency key replayed any number of times is one call.
  4. Envelope  - parse/signed_bytes/digest honour the asop.answer/1 contract.
  5. Clearance - narrow is commutative on grants and never widens; sees_space is
                 exactly the holder's space and its ancestors.

Stores are built per example inside a temp directory (function-scoped fixtures
and Hypothesis do not mix), on the jsonl and sqlite backends. Postgres is left
to the conformance suite: a server per example would cost more than the
property is worth there.

The profile is deterministic (`derandomize=True`) so CI is reproducible; raise
`AGENTCO_PROP_EXAMPLES` locally to search harder.
"""

from __future__ import annotations

import copy
import json
import os
import random
import tempfile
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine,
    initialize,
    invariant,
    rule,
    run_state_machine_as_test,
)

from agentco import clearance, envelope
from agentco.errors import Refusal
from agentco.sqlstore import SqlQueue
from agentco.work import Queue, WorkError, WorkStatus

EXAMPLES = int(os.environ.get("AGENTCO_PROP_EXAMPLES", "40"))
PROFILE = settings(
    derandomize=True,
    max_examples=EXAMPLES,
    deadline=None,
    database=None,
    suppress_health_check=list(HealthCheck),
)
MACHINE = settings(PROFILE, max_examples=max(10, EXAMPLES // 2), stateful_step_count=25)

KINDS = ("jsonl", "sqlite")
REFUSED = (WorkError, Refusal)
T0 = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
GATE = {"kind": "deterministic", "check": "make check", "max_park_seconds": 900, "on_timeout": "fail"}
ACTORS = ("alice", "bob", "carol")


def att(code: int) -> dict:
    return {"check": "make check", "exit_status": code, "environment": "ci/synthetic",
            "at": "2026-10-30T00:00:00+00:00"}


@contextmanager
def store(kind: str):
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        yield Queue(root / "work.jsonl") if kind == "jsonl" else SqlQueue(root / "agentco.sqlite3")


def snap(item) -> dict:
    """Everything the item says, as plain data (a refused call must not move any of it)."""
    return json.loads(item.to_json())


# --------------------------------------------------------------------------- #
# 1. fencing - a stateful model of leases, reaping, renewal and reports
# --------------------------------------------------------------------------- #


class LeaseMachine(RuleBasedStateMachine):
    kind = "jsonl"

    def __init__(self):
        super().__init__()
        self._cm = store(self.kind)
        self.queue = self._cm.__enter__()
        self.items: list[str] = []
        self.clock = 0
        self.seen_attempt: dict[str, int] = {}

    def teardown(self):
        self._cm.__exit__(None, None, None)

    @property
    def now(self) -> datetime:
        return T0 + timedelta(seconds=self.clock)

    @initialize()
    def first_item(self):
        self.new_item()

    @rule()
    def new_item(self):
        item = self.queue.create(f"item {len(self.items)}")
        self.items.append(item.id)
        self.seen_attempt[item.id] = item.lease_attempt

    @rule(seconds=st.integers(0, 400))
    def advance(self, seconds):
        self.clock += seconds

    @rule(n=st.integers(0, 5), actor=st.sampled_from(ACTORS), ttl=st.integers(1, 300))
    def claim(self, n, actor, ttl):
        item_id = self.items[n % len(self.items)]
        before = self.queue.get(item_id)
        won = self.queue.claim(item_id, actor, ttl_seconds=ttl, now=self.now)
        if won is None:
            assert snap(self.queue.get(item_id)) == snap(before), "a lost claim writes nothing"
            return
        assert not before.lease_active_at(self.now), "a claim never takes a live lease"
        assert won.lease_attempt == before.lease_attempt + 1
        assert won.leased_by == actor
        rival = next(a for a in ACTORS if a != actor)
        assert self.queue.claim(item_id, rival, ttl_seconds=ttl, now=self.now) is None, \
            "two actors never both hold a live lease"
        assert self.queue.get(item_id).leased_by == actor

    @rule()
    def reap(self):
        before = {i: self.queue.get(i) for i in self.items}
        self.queue.reap_expired_leases(self.now)
        for item_id, old in before.items():
            new = self.queue.get(item_id)
            if old.status is WorkStatus.IN_PROGRESS and old.leased_by and not old.lease_active_at(self.now):
                assert new.status is WorkStatus.PENDING and new.leased_by is None
                assert new.lease_attempt == old.lease_attempt + 1, "revoking a lease revokes its number"
            else:
                assert snap(new) == snap(old), "the reaper touches only expired leases"

    @rule(n=st.integers(0, 5), actor=st.sampled_from(ACTORS), delta=st.integers(-2, 2),
          status=st.sampled_from([WorkStatus.DONE, WorkStatus.FAILED]))
    def report(self, n, actor, delta, status):
        item_id = self.items[n % len(self.items)]
        before = self.queue.get(item_id)
        attempt = max(0, before.lease_attempt + delta)
        legit = (attempt == before.lease_attempt and before.leased_by == actor
                 and before.status is WorkStatus.IN_PROGRESS)
        try:
            after = self.queue.report_result(item_id, attempt, status, submitted_by=actor)
        except REFUSED:
            assert snap(self.queue.get(item_id)) == snap(before), "a refused report writes nothing"
            return
        assert legit, f"a report at attempt {attempt} by {actor} landed on {snap(before)}"
        assert after.status is status and after.leased_by is None
        assert after.lease_attempt == before.lease_attempt + 1, "the attempt ends with the lease"

    @rule(n=st.integers(0, 5), actor=st.sampled_from(ACTORS), delta=st.integers(-2, 2),
          ttl=st.integers(1, 300))
    def renew(self, n, actor, delta, ttl):
        item_id = self.items[n % len(self.items)]
        before = self.queue.get(item_id)
        attempt = max(0, before.lease_attempt + delta)
        legit = (attempt == before.lease_attempt and before.leased_by == actor
                 and before.status is WorkStatus.IN_PROGRESS and before.lease_active_at(self.now))
        try:
            after = self.queue.renew(item_id, attempt, actor, ttl_seconds=ttl, now=self.now)
        except REFUSED:
            assert snap(self.queue.get(item_id)) == snap(before), "a refused renewal writes nothing"
            return
        assert legit, f"a renewal at attempt {attempt} by {actor} landed on {snap(before)}"
        assert after.lease_attempt == before.lease_attempt, "a renewal never moves the fence"
        assert after.leased_by == actor and after.status is WorkStatus.IN_PROGRESS

    @invariant()
    def fence_never_decreases_and_lease_shape_holds(self):
        for item_id in self.items:
            item = self.queue.get(item_id)
            assert item.lease_attempt >= self.seen_attempt[item_id], "lease_attempt went backwards"
            self.seen_attempt[item_id] = item.lease_attempt
            if item.leased_by:
                assert item.status is WorkStatus.IN_PROGRESS and item.lease_expires_at
            else:
                assert item.lease_expires_at is None


@pytest.mark.parametrize("kind", KINDS)
def test_fencing_holds_for_any_interleaving(kind):
    machine = type(f"Lease_{kind}", (LeaseMachine,), {"kind": kind})
    run_state_machine_as_test(machine, settings=MACHINE)


# --------------------------------------------------------------------------- #
# 2. gates - verify_failures, stale verify_attempt, DONE is final
# --------------------------------------------------------------------------- #


class GateMachine(RuleBasedStateMachine):
    kind = "jsonl"

    def __init__(self):
        super().__init__()
        self._cm = store(self.kind)
        self.queue = self._cm.__enter__()
        item = self.queue.create("gated", verify=GATE)
        claimed = self.queue.claim(item.id, "alice", now=T0)
        landed = self.queue.report_result(item.id, claimed.lease_attempt, WorkStatus.DONE,
                                          attestation=att(1), submitted_by="alice")
        assert landed.status is WorkStatus.VERIFY_FAILED
        self.id = item.id
        self.failures = 1
        self.done = False

    def teardown(self):
        self._cm.__exit__(None, None, None)

    @rule(passed=st.booleans(), mode=st.sampled_from(["current", "low", "high", "absent"]),
          by=st.sampled_from(ACTORS))
    def attest(self, passed, mode, by):
        before = self.queue.get(self.id)
        attempt = {"current": self.failures, "low": self.failures - 1,
                   "high": self.failures + 1, "absent": None}[mode]
        assume(attempt is None or attempt >= 0)
        stale = attempt is not None and attempt != self.failures
        try:
            after = self.queue.attest(self.id, att(0 if passed else 1), by, verify_attempt=attempt)
        except REFUSED:
            assert self.done or stale, "a live, current answer was refused"
            assert snap(self.queue.get(self.id)) == snap(before), "a refused answer writes nothing"
            return
        assert not self.done, "an answer changed a DONE item"
        assert not stale, "a stale verify_attempt landed"
        if passed:
            assert after.status is WorkStatus.DONE
            self.done = True
        else:
            self.failures += 1
            assert after.status is WorkStatus.VERIFY_FAILED
        assert after.verify_failures == self.failures, "one failing verdict, one increment"

    @invariant()
    def counters_agree(self):
        item = self.queue.get(self.id)
        assert item.verify_failures == self.failures
        assert (item.status is WorkStatus.DONE) == self.done


@pytest.mark.parametrize("kind", KINDS)
def test_gates_for_any_interleaving_of_verdicts(kind):
    machine = type(f"Gate_{kind}", (GateMachine,), {"kind": kind})
    run_state_machine_as_test(machine, settings=MACHINE)


# --------------------------------------------------------------------------- #
# 3. idempotency - a key replayed N times is one call
# --------------------------------------------------------------------------- #

KEYS = st.text(alphabet="abcdefghij-0123456789", min_size=1, max_size=12)


@pytest.mark.parametrize("kind", KINDS)
@PROFILE
@given(key=KEYS, replays=st.integers(1, 5), result=st.one_of(st.none(), st.text(max_size=20)))
def test_report_replays_equal_one_call(kind, key, replays, result):
    with store(kind) as queue:
        item = queue.create("work")
        claimed = queue.claim(item.id, "alice", now=T0)
        first = queue.report_result(item.id, claimed.lease_attempt, WorkStatus.DONE, result=result,
                                    idempotency_key=key, submitted_by="alice")
        once = snap(queue.get(item.id))
        for _ in range(replays):
            # The honest retry names the same attempt; a lost-ack retry must
            # not be told it was superseded, and must not rewrite anything.
            again = queue.report_result(item.id, claimed.lease_attempt, WorkStatus.DONE, result=result,
                                        idempotency_key=key, submitted_by="alice")
            assert snap(again) == once
        assert snap(first)["status"] == once["status"]
        assert snap(queue.get(item.id)) == once


@pytest.mark.parametrize("kind", KINDS)
@PROFILE
@given(key=KEYS, replays=st.integers(1, 5), passed=st.booleans(), replay_passed=st.booleans())
def test_attest_replays_equal_one_call(kind, key, replays, passed, replay_passed):
    with store(kind) as queue:
        item = queue.create("gated", verify=GATE)
        claimed = queue.claim(item.id, "alice", now=T0)
        queue.report_result(item.id, claimed.lease_attempt, WorkStatus.DONE, attestation=att(1),
                            submitted_by="alice")
        queue.attest(item.id, att(0 if passed else 1), "bob", idempotency_key=key, verify_attempt=1)
        once = snap(queue.get(item.id))
        for _ in range(replays):
            # Same key, same submitter: the recorded answer comes back even if
            # the replay's body differs or its verify_attempt has gone stale.
            again = queue.attest(item.id, att(0 if replay_passed else 1), "bob",
                                 idempotency_key=key, verify_attempt=1)
            assert snap(again) == once
        assert snap(queue.get(item.id)) == once


@pytest.mark.parametrize("kind", KINDS)
@PROFILE
@given(natural_key=KEYS, replays=st.integers(1, 5),
       titles=st.lists(st.text(min_size=1, max_size=15), min_size=1, max_size=5))
def test_create_with_a_natural_key_is_one_item(kind, natural_key, replays, titles):
    with store(kind) as queue:
        first = queue.create(titles[0], natural_key=natural_key)
        once = snap(queue.get(first.id))
        for title in (titles * replays)[:replays]:
            again = queue.create(title, natural_key=natural_key)
            assert again.id == first.id
        assert [i.id for i in queue.list()] == [first.id]
        assert snap(queue.get(first.id)) == once


# --------------------------------------------------------------------------- #
# 4. the signed-answer envelope contract
# --------------------------------------------------------------------------- #

needs_canonical_json = pytest.mark.skipif(not envelope.SUPPORTED, reason="asop-spec predates canonical JSON")

HEX32 = st.text(alphabet="0123456789abcdef", min_size=32, max_size=32)
COUNT = st.integers(0, 2**53)
TEXT = st.text(min_size=1, max_size=20)
PAYLOADS = st.fixed_dictionaries({
    "type": st.just(envelope.TYPE),
    "hub": HEX32,
    "item": TEXT,
    "verify_attempt": COUNT,
    "stage": st.one_of(st.none(), COUNT),
    "epoch": COUNT,
    "verdict": st.fixed_dictionaries({"passed": st.booleans(), "reason": st.text(max_size=30)}),
    "choice": st.one_of(st.none(), st.text(max_size=20)),
    "signer": TEXT,
    "key_id": TEXT,
    "nonce": HEX32,
    "issued_at": COUNT,
})


def reserialize(payload: dict, seed: int, ascii_only: bool, indent) -> str:
    """The same JSON value, written with its keys shuffled and its whitespace varied."""
    rng = random.Random(seed)

    def shuffled(value):
        if isinstance(value, dict):
            keys = list(value)
            rng.shuffle(keys)
            return {k: shuffled(value[k]) for k in keys}
        return value

    seps = (", ", ": ") if indent is None else (",", ": ")
    return json.dumps(shuffled(payload), ensure_ascii=ascii_only, indent=indent, separators=seps)


@needs_canonical_json
@PROFILE
@given(payload=PAYLOADS, seed=st.integers(0, 2**32), ascii_only=st.booleans(),
       indent=st.sampled_from([None, 0, 1, 4]))
def test_parse_round_trips_and_signed_bytes_ignore_serialization(payload, seed, ascii_only, indent):
    text = reserialize(payload, seed, ascii_only, indent)
    parsed = envelope.parse(text)
    assert parsed == payload
    assert envelope.parse(json.dumps(payload)) == payload
    assert envelope.signed_bytes(parsed) == envelope.signed_bytes(payload)
    assert envelope.digest(parsed) == envelope.digest(payload)
    assert envelope.signed_bytes(payload).startswith(envelope.TYPE.encode() + b"\n")


@needs_canonical_json
@PROFILE
@given(a=PAYLOADS, b=PAYLOADS)
def test_digest_differs_whenever_any_payload_differs(a, b):
    assume(a != b)
    assert envelope.digest(a) != envelope.digest(b)
    assert envelope.signed_bytes(a) != envelope.signed_bytes(b)


@needs_canonical_json
@PROFILE
@given(payload=PAYLOADS, data=st.data())
def test_changing_exactly_one_field_changes_the_digest(payload, data):
    field = data.draw(st.sampled_from([f for f in envelope.FIELDS if f != "type"]))
    changed = copy.deepcopy(payload)
    replacement = data.draw(PAYLOADS)[field]
    assume(replacement != payload[field])
    changed[field] = replacement
    assert envelope.digest(changed) != envelope.digest(payload)


@needs_canonical_json
@PROFILE
@given(payload=PAYLOADS, extra=st.text(alphabet="abcxyz_", min_size=1, max_size=8))
def test_a_payload_with_an_unknown_or_missing_field_is_refused(payload, extra):
    assume(extra not in envelope.FIELDS)
    with pytest.raises(Refusal) as unknown:
        envelope.parse(json.dumps({**payload, extra: 1}))
    assert unknown.value.code == "envelope_invalid"
    dropped = {k: v for k, v in payload.items() if k != "nonce"}
    with pytest.raises(Refusal) as missing:
        envelope.parse(json.dumps(dropped))
    assert missing.value.code == "envelope_invalid"


# --------------------------------------------------------------------------- #
# 5. clearance narrowing and spaces
# --------------------------------------------------------------------------- #

LEVELS = clearance.DEFAULT_LEVELS
OWNER_NAMES = ("acme", "personal", "agency", "lab")
SEGMENT = st.from_regex(r"[A-Za-z0-9][A-Za-z0-9._-]{0,5}", fullmatch=True)
SPACES = st.lists(SEGMENT, max_size=clearance.MAX_SPACE_DEPTH).map("/".join)
CLEARANCES = st.builds(
    lambda grants, space: clearance.Clearance(grants, LEVELS, space=space),
    st.dictionaries(st.sampled_from(OWNER_NAMES), st.sampled_from(LEVELS), max_size=4),
    SPACES,
)


@PROFILE
@given(a=CLEARANCES, b=CLEARANCES)
def test_narrow_is_commutative_on_grants_and_never_widens(a, b):
    ab, ba = a.narrow(b), b.narrow(a)
    assert dict(ab.grants) == dict(ba.grants)
    assert set(ab.grants) == set(a.grants) & set(b.grants), "an owner one side lacks is not granted"
    for owner in OWNER_NAMES:
        for level in LEVELS:
            if ab.allows(owner, level):
                assert a.allows(owner, level) and b.allows(owner, level)


@PROFILE
@given(a=CLEARANCES, b=CLEARANCES, c=CLEARANCES)
def test_narrow_is_associative_and_idempotent_on_grants(a, b, c):
    assert dict(a.narrow(a).grants) == dict(a.grants)
    assert dict(a.narrow(b).narrow(c).grants) == dict(a.narrow(b.narrow(c)).grants)


@PROFILE
@given(holder=SPACES, other=SPACES)
def test_sees_space_is_exactly_the_holders_space_and_its_ancestors(holder, other):
    held = clearance.Clearance({}, LEVELS, space=holder)
    segments = holder.split("/") if holder else []
    ancestors = {"/".join(segments[:i]) for i in range(len(segments) + 1)}  # "" is the root
    assert held.sees_space(other) == (other in ancestors)
    assert held.sees_space(holder) and held.sees_space(clearance.ROOT_SPACE)
    # never a child or a sibling
    assert not held.sees_space(holder + "/child" if holder else "child")
