"""The outbox — L1's write path, its refusals, and its receipts.

What is being defended here is the experience of a harness that configured
nothing: it appends a line to a file and gets told what happened. Every test in
this file is about one of the three ways that promise breaks — the line is
silently dropped, the line is delivered twice, or the line is refused and nobody
ever finds out.

`tests/test_outbox_concurrency.py` carries the same contract across real OS
processes, which is where the file-locking claims are actually settled.
"""

from __future__ import annotations

import json
import time
from typing import Optional

import pytest
from fastapi.testclient import TestClient

from agentco import auth
from agentco.app import create_app
from agentco.errors import Refusal
from agentco.outbox import (
    MAX_LINE_BYTES,
    PENDING_VERBS,
    PUSH_VERBS,
    RECEIPT_HISTORY,
    Outbox,
    drain,
    registry_publisher,
    resolve_node_dir,
    validate_line,
)
from agentco.publish import Registry

CLAIM = {"repo": "acme/app", "prefixes": ["src/api/handlers"], "intent": "implement"}


@pytest.fixture
def box(tmp_path) -> Outbox:
    return Outbox(tmp_path / ".agentco")


def publisher(recorder: Optional[list] = None, outcome=None):
    """A publisher that records what it was asked to send.

    Injected rather than mocked-out HTTP: the drainer's own behaviour —
    ordering, settling, receipts, the crash windows — is what these tests are
    about, and a real server standing in the middle would only add a way for
    them to fail for an unrelated reason. The end-to-end test at the bottom of
    this file is where the wire is real.
    """

    def publish(record: dict) -> dict:
        if recorder is not None:
            recorder.append(record)
        if callable(outcome):
            return outcome(record)
        return {"leaseUid": "lease-1", "conflicts": []}

    return publish


# --------------------------------------------------------------------------- #
# The write boundary
# --------------------------------------------------------------------------- #


def test_the_push_set_carries_reports_and_not_filings():
    """"Any tool may push. No tool may file." Filing work into somebody's queue
    has a consequence for a person who did not ask for it, and the cheapest
    write path — a file anybody with repo access can append to — is not where
    that belongs."""
    assert "work_create" not in PUSH_VERBS
    assert set(PUSH_VERBS) == {"claim_scope", "release_scope", "snapshot", "work_report"}


def test_a_reserved_verb_is_refused_as_not_yet_rather_than_never(box):
    """`attest` belongs in the push set and has no transport. The refusal has to
    say which, because "not yet" and "never" are different instructions."""
    with pytest.raises(Refusal) as caught:
        box.push(PENDING_VERBS[0], {"itemId": "w-1"})
    assert "no transport yet" in caught.value.message
    assert "Hold this line" in caught.value.remediation


def test_an_unknown_verb_is_refused_with_the_push_set_named(box):
    with pytest.raises(Refusal) as caught:
        box.push("delete_everything", {})
    assert "claim_scope" in caught.value.message


def test_a_line_may_not_name_its_own_actor(box):
    """The rule that holds on every transport, this one included. The outbox is
    local IPC: whoever can write the repo can write this file, so a self-named
    actor here would be an identity anybody could assert."""
    with pytest.raises(Refusal) as caught:
        box.push("claim_scope", dict(CLAIM, actor="somebody-trusted"))
    assert caught.value.message == "payload sets `actor`"
    assert "signature decides the actor" in caught.value.remediation
    assert box.pending() == []


def test_an_unknown_line_field_is_refused_rather_than_ignored():
    with pytest.raises(Refusal) as caught:
        validate_line(
            {
                "line_id": "ob_1",
                "at": "2026-09-01T00:00:00+00:00",
                "verb": "claim_scope",
                "payload": CLAIM,
                "agent_label": None,
                "priority": "urgent",
            }
        )
    assert "unknown outbox line field(s) ['priority']" in caught.value.message


def test_a_body_sized_payload_is_refused(box):
    """The plane stores pointers, never documents. Refusing here is cheaper than
    refusing at the far end of an HTTP call the agent will never see the result
    of."""
    with pytest.raises(Refusal) as caught:
        box.push("snapshot", {"artifactUri": "git:/r#main", "purpose": "x" * MAX_LINE_BYTES})
    assert "over the" in caught.value.message
    assert "pointer, not a body" in caught.value.remediation


def test_a_pushed_line_is_one_json_object_per_line(box):
    box.push("claim_scope", CLAIM, agent_label="cursor")
    box.push("snapshot", {"artifactUri": "git:/r#main", "purpose": "baseline"})
    raw = box.path.read_bytes().decode().strip().split("\n")
    assert len(raw) == 2
    first = json.loads(raw[0])
    assert first["verb"] == "claim_scope"
    assert first["agent_label"] == "cursor"
    assert "actor" not in first


def test_the_node_dir_default_is_relative_to_the_repo(monkeypatch):
    """Absolute would put every harness on one machine into a single file, where
    one agent's malformed line becomes everybody's quarantine."""
    monkeypatch.delenv("AGENTCO_NODE_DIR", raising=False)
    assert not resolve_node_dir().is_absolute()
    monkeypatch.setenv("AGENTCO_NODE_DIR", "/srv/node")
    assert str(resolve_node_dir()) == "/srv/node"
    assert str(resolve_node_dir("/explicit")) == "/explicit"


# --------------------------------------------------------------------------- #
# Draining, and what the agent is told afterwards
# --------------------------------------------------------------------------- #


def test_a_drained_line_is_published_once_and_removed(box):
    sent: list[dict] = []
    line = box.push("claim_scope", CLAIM)
    result = drain(box, publisher(sent))
    assert result["published"] == 1
    assert [r["verb"] for r in sent] == ["claim_scope"]
    assert box.pending() == []
    [receipt] = box.read_receipts()
    assert receipt["line_id"] == line["line_id"]
    assert receipt["state"] == "published"


def test_a_refusal_is_terminal_and_carries_its_remediation(box):
    """A refused line is not retried — the registry will refuse it every time —
    and the receipt is the only place the agent that wrote it can learn why. It
    has already exited."""
    box.push("claim_scope", CLAIM)

    def refuse(record):
        raise Refusal(code="scope_too_broad", message="one segment", remediation="Name a deeper path.")

    result = drain(box, publisher(outcome=refuse))
    assert result["refused"] == 1
    assert box.pending() == [], "a refused line must not be retried forever"
    [receipt] = box.read_receipts()
    assert receipt["state"] == "refused"
    assert receipt["code"] == "scope_too_broad"
    assert receipt["remediation"] == "Name a deeper path."


def test_a_transport_failure_is_retried_and_is_not_a_refusal(box):
    """"The registry said no" and "the registry was not there" prompt completely
    different responses from whoever reads the receipt, so they are never the
    same state."""
    box.push("claim_scope", CLAIM)

    def boom(record):
        raise ConnectionError("connection refused")

    result = drain(box, publisher(outcome=boom))
    assert result["retryable"] == 1 and result["refused"] == 0
    assert len(box.pending()) == 1, "a line the registry never saw must survive"
    assert box.read_receipts()[-1]["state"] == "retryable"

    # And the retry succeeds without the caller doing anything special.
    assert drain(box, publisher())["published"] == 1
    assert box.pending() == []


def test_a_malformed_line_is_quarantined_with_a_reason_and_a_receipt(box):
    """An agent that writes garbage must find out. Quarantine preserves the
    bytes; the RECEIPT is what anybody actually reads."""
    box.push("claim_scope", CLAIM)
    with open(box.path, "a") as handle:
        handle.write('{"line_id":"ob_bad","at":"t","verb":"work_create","payload":{}}\n')
        handle.write('{"line_id":"ob_trunc","at":"t","verb":"snap')  # died mid-write

    result = drain(box, publisher())
    assert result["published"] == 1
    assert result["quarantined"] == 2
    states = {r["state"] for r in box.read_receipts()}
    assert states == {"published", "quarantined"}

    reasons = " ".join(r.get("detail", "") for r in box.read_receipts())
    assert "Any tool may push" in reasons, "the verb refusal names the rule"
    assert "not parseable" in reasons, "the truncated line says what it looks like"

    # The bytes survive, and the outbox is left clean.
    assert box.quarantine_path.exists()
    assert box.pending() == []
    assert drain(box, publisher())["quarantined"] == 0, "quarantine happens once, not every run"


def test_a_byte_that_is_not_utf8_costs_one_line_and_not_the_file(box):
    """The reason the outbox is read as BYTES and decoded per line.

    Decoding the whole file at once puts `UnicodeDecodeError` outside the
    per-line handler — and it is a `ValueError`, not a `JSONDecodeError`, so it
    escapes every `except json.JSONDecodeError` in the module and takes the
    entire drain with it. One stray byte from a mis-encoded external tool then
    costs every line in the file, which is the opposite of what quarantining is
    for.

    Written after a mutation survived: `_read_raw`'s docstring made this exact
    claim and nothing tested it, so replacing the byte-wise read with
    `read_text()` passed the whole suite.
    """
    good = [box.push("claim_scope", CLAIM)["line_id"] for _ in range(2)]
    with open(box.path, "ab") as handle:
        handle.write(b'{"line_id":"ob_latin1","at":"t","verb":"claim_scope","payload":{"repo":"\xff\xfe"}}\n')
    more = box.push("snapshot", {"artifactUri": "git:/r#main", "purpose": "after the bad byte"})

    published: list[str] = []
    result = drain(box, publisher(outcome=lambda r: published.append(r["line_id"]) or {"leaseUid": "l"}))

    assert result["quarantined"] == 1
    assert sorted(published) == sorted(good + [more["line_id"]]), (
        "the lines after the undecodable one must still be delivered"
    )
    assert box.pending() == []


def test_one_bad_line_does_not_strand_the_rest_of_the_batch(box):
    for _ in range(3):
        box.push("claim_scope", CLAIM)
    with open(box.path, "a") as handle:
        handle.write("not json at all\n")
    result = drain(box, publisher())
    assert result["published"] == 3 and result["quarantined"] == 1


def test_a_line_appended_during_a_drain_is_not_lost(box):
    """The batch is claimed but not removed up front, and `settle` re-reads the
    file under the lock. A drain that assumed the file it read was still the
    file it was writing would delete this line without sending it."""
    box.push("claim_scope", CLAIM)
    late: list[str] = []

    def publish_then_append(record):
        if not late:
            late.append(box.push("snapshot", {"artifactUri": "git:/r#main", "purpose": "late"})["line_id"])
        return {"leaseUid": "lease-1"}

    drain(box, publisher(outcome=publish_then_append))
    pending = box.pending()
    assert [r["line_id"] for r in pending] == late
    assert drain(box, publisher())["published"] == 1


def test_a_second_drainer_does_nothing_and_says_so(box):
    """Two drainers is an overlapping schedule, not an error. The second one
    must not queue up behind the first and then publish into a file the first
    has already read."""
    box.push("claim_scope", CLAIM)
    with box.drain_lock() as acquired:
        assert acquired
        result = drain(box, publisher())
    assert result["state"] == "skipped"
    assert result["published"] == 0
    assert len(box.pending()) == 1, "nothing was consumed by the skipped run"


def test_work_report_carries_the_line_id_as_its_idempotency_key(box):
    """This is what makes at-least-once cheap. A drainer that died after the
    HTTP call and before settling republishes, and the registry returns the
    recorded outcome instead of recording a second one."""
    sent: list[dict] = []

    class FakeRegistry:
        def work_report(self, item_id, attempt, status, result=None, idempotency_key=None):
            sent.append({"itemId": item_id, "idempotencyKey": idempotency_key})
            return {"id": item_id, "status": status}

    line = box.push("work_report", {"itemId": "w-1", "attempt": 1, "status": "done"})
    drain(box, registry_publisher(FakeRegistry()))
    assert sent == [{"itemId": "w-1", "idempotencyKey": line["line_id"]}]


def test_receipts_are_bounded(box):
    for _ in range(RECEIPT_HISTORY + 20):
        box.push("claim_scope", CLAIM)
        drain(box, publisher())
    assert len(box.read_receipts()) == RECEIPT_HISTORY


def test_a_receipt_carries_identifiers_and_not_bodies(box):
    """Receipts are surfaced in a spliced context block, where every byte is
    paid for by every conversation in the repo."""
    box.push("claim_scope", CLAIM)
    drain(box, publisher(outcome=lambda r: {"leaseUid": "lease-1", "conflicts": [], "prose": "x" * 5000}))
    [receipt] = box.read_receipts()
    assert receipt["result"] == {"leaseUid": "lease-1"}


def test_an_unreadable_watermark_does_not_stop_a_drain(box):
    """The watermark is an observation about the last run, never the authority
    for what to send — the outbox file is that."""
    box.push("claim_scope", CLAIM)
    box.dir.mkdir(parents=True, exist_ok=True)
    box.watermark_path.write_bytes(b"\xff\xfe not json")
    assert drain(box, publisher())["published"] == 1


# --------------------------------------------------------------------------- #
# End to end, over the real wire
# --------------------------------------------------------------------------- #


KEYS = {"bigmac": "bigmac-secret"}


class LoopbackRegistry(Registry):
    """A real `Registry` whose transport is the real app, in process.

    Same device as `tests/test_mcp_remote.py`: real signing, real handlers, real
    refusals. The claim being tested is that a line written by an agent with no
    credential arrives at the registry signed by the MACHINE, with the agent's
    self-reported label attached and unverified — and that is a claim about the
    whole path, so nothing in the middle of it may be a stand-in.
    """

    def __init__(self, actor: str, client: TestClient):
        super().__init__(actor, KEYS[actor], "http://registry.test")
        self.client = client

    def _call(self, method: str, path: str, body: Optional[dict] = None, query: str = "") -> dict:
        raw = json.dumps(body).encode() if body is not None else b""
        ts = str(int(time.time()))
        response = self.client.request(
            method,
            f"{path}{query}",
            content=raw if raw else None,
            headers={
                "X-AgentCo-Actor": self.actor,
                "X-AgentCo-Timestamp": ts,
                "X-AgentCo-Signature": auth.sign(self.secret, method, path, ts, raw),
                "Content-Type": "application/json",
            },
        )
        if response.status_code >= 400:
            from agentco.publish import RegistryError

            raise RegistryError(response.status_code, response.json())
        return response.json()


def test_a_line_from_an_unconfigured_harness_reaches_the_registry(tmp_path):
    """The whole of L1, end to end: append a line holding no credential, run the
    drainer, and the claim is in the registry under the machine's identity with
    the harness named as unverified."""
    app = create_app(db_path=str(tmp_path / "registry.sqlite3"), keys=KEYS)
    client = TestClient(app)
    registry = LoopbackRegistry("bigmac", client)

    box = Outbox(tmp_path / ".agentco")
    box.push("claim_scope", CLAIM, agent_label="cursor")

    result = drain(box, registry_publisher(registry))
    assert result["published"] == 1, result

    events = registry.events()["events"]
    [claimed] = [e for e in events if e["kind"] == "ScopeClaimed"]
    assert claimed["actor"] == "bigmac", "the drainer's credential decides the actor"
    assert claimed["agentLabel"] == "cursor", "the harness is named, and unverified"


def test_a_registry_refusal_comes_back_as_a_refusal_not_a_retry(tmp_path):
    """A too-broad prefix is refused by the registry every time. Recording that
    as a transport failure would retry it on every drain forever."""
    app = create_app(db_path=str(tmp_path / "registry.sqlite3"), keys=KEYS)
    registry = LoopbackRegistry("bigmac", TestClient(app))

    box = Outbox(tmp_path / ".agentco")
    box.push("claim_scope", {"repo": "acme/app", "prefixes": ["src"], "intent": "implement"})

    result = drain(box, registry_publisher(registry))
    assert result["refused"] == 1 and result["retryable"] == 0
    assert box.pending() == []
    receipt = box.read_receipts()[-1]
    assert receipt["code"] == "scope_too_broad"
    assert "segments" in receipt["remediation"]
