"""`memory_create` in the outbox's push set: a lesson learned on an unconfigured
harness reaches the hub's shared memory through the same file as everything else.

What is defended here:

  * the line is validated at the write boundary — `provenance` is mandatory, an
    identity in the payload is refused, an unknown key is refused (the drainer's
    mapping would otherwise drop it silently);
  * the hub stays the authority — labels, clearance, memory-off — and the outbox
    adds no bypass: a refusal over HTTP and the same refusal through the outbox
    carry the same code;
  * delivery is at-least-once, so the line id is the idempotency key and a
    replayed line creates no second item.

Synthetic data only.
"""

from __future__ import annotations

import json
import time
from typing import Optional

import pytest
from fastapi.testclient import TestClient

from agentco import auth
from agentco.app import ROUTE_BODIES, create_app
from agentco.errors import Refusal
from agentco.outbox import PUSH_VERBS, Outbox, drain, registry_publisher, validate_line
from agentco.publish import Registry, RegistryError

VERB = "memory_create"
KEYS = {"dana": "dana-secret", "kofi": "kofi-secret", "operator": "op-secret"}
CLEARANCES = {"dana": {"acme": "confidential"}, "kofi": {"acme": "internal"}}
LEVELS = ("all", "internal", "confidential", "private")
PROV = {"kind": "run", "ref": "run:w-0001"}


def memory_payload(**over) -> dict:
    base = {
        "label": {"owner": "acme", "level": "internal"},
        "layer": "shared",
        "title": "Retry the export once",
        "body": "A stalled export resumes after one retry.",
        "kind": "lesson",
        "tags": ["export"],
        "provenance": PROV,
    }
    base.update(over)
    return base


class Loopback(Registry):
    """A real `Registry` whose transport is the real app, in process."""

    def __init__(self, actor: str, client: TestClient):
        super().__init__(actor, KEYS[actor], "http://registry.test", via="outbox")
        self.client = client

    def _call(self, method: str, path: str, body: Optional[dict] = None, query: str = "") -> dict:
        raw = json.dumps(body).encode() if body is not None else b""
        ts = str(int(time.time()))
        response = self.client.request(method, f"{path}{query}", content=raw or None, headers={
            "X-AgentCo-Actor": self.actor, "X-AgentCo-Timestamp": ts,
            "X-AgentCo-Signature": auth.sign(self.secret, method, path, ts, raw, query=query.lstrip("?") or None),
            "Content-Type": "application/json", "X-AgentCo-Via": "outbox",
        })
        if response.status_code >= 400:
            raise RegistryError(response.status_code, response.json())
        return response.json()


def make_app(tmp_path, memory: bool = True) -> TestClient:
    return TestClient(create_app(
        db_path=str(tmp_path / "api.sqlite3"), keys=KEYS, operator="operator",
        work_store=str(tmp_path / "work.jsonl"), sop_store=str(tmp_path / "sops.jsonl"),
        memory_store=str(tmp_path / "memory.jsonl"), hub_id="hub-a", humans=["dana"],
        hub_clearance={"acme": "confidential"}, clearances=CLEARANCES, memory_levels=LEVELS,
        memory_enabled=memory,
    ))


def http_post(client, path, actor, body):
    raw = json.dumps(body).encode()
    ts = str(int(time.time()))
    return client.post(path, content=raw, headers={
        "X-AgentCo-Actor": actor, "X-AgentCo-Timestamp": ts,
        "X-AgentCo-Signature": auth.sign(KEYS[actor], "POST", path, ts, raw),
        "Content-Type": "application/json",
    })


def http_get(client, path, actor, query=""):
    ts = str(int(time.time()))
    return client.get(f"{path}{query}", headers={
        "X-AgentCo-Actor": actor, "X-AgentCo-Timestamp": ts,
        "X-AgentCo-Signature": auth.sign(KEYS[actor], "GET", path, ts, b"", query=query.lstrip("?") or None),
    })


@pytest.fixture
def box(tmp_path) -> Outbox:
    return Outbox(tmp_path / "box" / ".agentco")


@pytest.fixture
def hub(tmp_path):
    return make_app(tmp_path)


def items_for(client, actor="dana") -> list[dict]:
    return http_get(client, "/memory", actor).json()["items"]


# --------------------------------------------------------------------------- #
# the write boundary
# --------------------------------------------------------------------------- #


def test_the_verb_is_in_the_push_set_and_a_complete_line_validates(box):
    assert VERB in PUSH_VERBS
    line = box.push(VERB, memory_payload(), agent_label="aider")
    assert validate_line(line)["verb"] == VERB


def test_a_line_without_provenance_is_refused_and_never_queued(box):
    payload = memory_payload()
    del payload["provenance"]
    with pytest.raises(Refusal) as caught:
        box.push(VERB, payload)
    assert "provenance" in caught.value.message
    assert box.pending() == []


@pytest.mark.parametrize("bad", [None, "run:w-1", ["run", "w-1"]])
def test_a_provenance_that_is_not_an_object_is_refused(box, bad):
    with pytest.raises(Refusal):
        box.push(VERB, memory_payload(provenance=bad))


@pytest.mark.parametrize("key", ["actor", "author", "author_kind", "created_by", "updated_by"])
def test_a_payload_naming_who_wrote_it_is_refused(box, key):
    """The drainer's signature decides the actor, here as everywhere."""
    with pytest.raises(Refusal) as caught:
        box.push(VERB, memory_payload(**{key: "somebody-trusted"}))
    assert key in caught.value.message
    assert box.pending() == []


def test_an_unknown_payload_key_is_refused_not_dropped(box):
    """`registry_publisher` forwards named fields only, so an unread key would be
    one the sender believes it sent."""
    with pytest.raises(Refusal) as caught:
        box.push(VERB, memory_payload(provenence=PROV))
    assert "provenence" in caught.value.message


def test_a_hand_written_line_without_provenance_is_quarantined_with_a_receipt(box):
    payload = memory_payload()
    del payload["provenance"]
    box.dir.mkdir(parents=True, exist_ok=True)
    box.path.write_text(json.dumps({"line_id": "ob_1", "at": "2026-01-01T00:00:00+00:00",
                                    "verb": VERB, "payload": payload}) + "\n")
    sent: list = []
    result = drain(box, lambda record: sent.append(record) or {})
    assert sent == [] and result["quarantined"] == 1
    assert "provenance" in box.read_receipts()[-1]["detail"]


def test_the_allowed_payload_keys_are_exactly_the_wire_body_of_post_memory():
    """One shape, not a second: the outbox payload IS the `POST /memory` body."""
    from agentco.outbox import MEMORY_CREATE_KEYS

    wire = set(ROUTE_BODIES[("POST", "/memory")].keys)
    assert set(MEMORY_CREATE_KEYS) == wire - {"idempotencyKey"}
    assert "idempotencyKey" in wire


# --------------------------------------------------------------------------- #
# the mapping onto the signed client
# --------------------------------------------------------------------------- #


def test_publish_maps_the_line_onto_the_client_with_the_line_id_as_the_key(box):
    sent: list[dict] = []

    class FakeRegistry:
        def memory_create(self, **kwargs):
            sent.append(kwargs)
            return {"state": "accepted", "item": {"item_id": "mem-1", "version": 1}}

    payload = memory_payload(applies_to="export", space="team/a")
    line = box.push(VERB, payload, agent_label="aider")
    result = drain(box, registry_publisher(FakeRegistry()))
    assert result["published"] == 1
    (call,) = sent
    assert call["idempotency_key"] == line["line_id"]
    assert call["agent_label"] == "aider"
    for key, value in payload.items():
        assert call[key] == value, key


def test_the_receipt_names_the_item_and_its_version(box):
    class FakeRegistry:
        def memory_create(self, **kwargs):
            return {"state": "accepted", "item": {"item_id": "mem-9", "version": 1, "body": "x" * 500}}

    box.push(VERB, memory_payload())
    receipt = drain(box, registry_publisher(FakeRegistry()))["receipts"][0]
    assert receipt["state"] == "published"
    assert receipt["result"] == {"state": "accepted", "item_id": "mem-9", "version": 1}, "ids, not the body"


# --------------------------------------------------------------------------- #
# end to end, through the real app
# --------------------------------------------------------------------------- #


def test_a_line_reaches_the_hub_and_is_authored_by_the_drainers_credential(hub, box):
    box.push(VERB, memory_payload(), agent_label="aider")
    result = drain(box, registry_publisher(Loopback("kofi", hub)))
    assert (result["published"], result["refused"], result["retryable"]) == (1, 0, 0)
    (item,) = items_for(hub, "kofi")
    assert item["created_by"] == "kofi"
    assert item["label"] == {"owner": "acme", "level": "internal"} or item["owner"] == "acme"
    assert item["provenance"] == PROV
    assert item["version"] == 1


def test_republishing_one_line_creates_one_item(hub, box):
    publish = registry_publisher(Loopback("kofi", hub))
    line = box.push(VERB, memory_payload())
    first, second = publish(line), publish(line)
    assert first["item"]["item_id"] == second["item"]["item_id"]
    assert len(items_for(hub, "kofi")) == 1


def test_a_second_line_with_its_own_id_is_a_second_item(hub, box):
    publish = registry_publisher(Loopback("kofi", hub))
    publish(box.push(VERB, memory_payload()))
    publish(box.push(VERB, memory_payload()))
    assert len(items_for(hub, "kofi")) == 2


def test_one_actors_key_is_not_another_actors_replay(hub, box):
    """Keys are scoped (actor, verb, key): reusing someone's line id is neither a
    way to read their outcome nor to pre-empt their write."""
    line = box.push(VERB, memory_payload())
    first = registry_publisher(Loopback("kofi", hub))(line)
    second = registry_publisher(Loopback("dana", hub))(line)
    assert first["item"]["item_id"] != second["item"]["item_id"]


def test_http_takes_an_idempotency_key_and_replays_the_recorded_item(hub):
    body = {**memory_payload(), "idempotencyKey": "k-1"}
    a = http_post(hub, "/memory", "kofi", body)
    b = http_post(hub, "/memory", "kofi", body)
    assert a.status_code == b.status_code == 200
    assert a.json()["item"]["item_id"] == b.json()["item"]["item_id"]
    assert len(items_for(hub, "kofi")) == 1


def test_the_key_is_not_stored_on_the_item(hub):
    item = http_post(hub, "/memory", "kofi", {**memory_payload(), "idempotencyKey": "k-2"}).json()["item"]
    assert "k-2" not in json.dumps(item)


def test_http_refuses_a_bad_idempotency_key(hub):
    response = http_post(hub, "/memory", "kofi", {**memory_payload(), "idempotencyKey": ""})
    assert response.status_code == 400
    assert response.json()["code"] == "bad_idempotency_key"
    assert items_for(hub, "kofi") == []


def test_http_without_a_key_still_creates_each_time(hub):
    http_post(hub, "/memory", "kofi", memory_payload())
    http_post(hub, "/memory", "kofi", memory_payload())
    assert len(items_for(hub, "kofi")) == 2


# --------------------------------------------------------------------------- #
# the hub stays the authority
# --------------------------------------------------------------------------- #


def test_memory_off_settles_as_a_refusal_with_the_hubs_code(tmp_path, box):
    off = make_app(tmp_path, memory=False)
    box.push(VERB, memory_payload())
    result = drain(box, registry_publisher(Loopback("kofi", off)))
    assert (result["published"], result["refused"], result["retryable"]) == (0, 1, 0)
    assert result["receipts"][0]["code"] == "memory_disabled"
    assert box.pending() == [], "a refusal is terminal: not retried forever"
    # and the next drain has nothing to do, rather than crashing or looping
    assert drain(box, registry_publisher(Loopback("kofi", off)))["receipts"] == []


def test_a_label_above_the_credentials_clearance_is_refused_as_over_http(hub, box):
    over = memory_payload(label={"owner": "acme", "level": "confidential"})
    direct = http_post(hub, "/memory", "kofi", over)
    assert direct.status_code >= 400

    box.push(VERB, over)
    result = drain(box, registry_publisher(Loopback("kofi", hub)))
    assert (result["published"], result["refused"]) == (0, 1)
    assert result["receipts"][0]["code"] == direct.json()["code"]
    assert items_for(hub, "dana") == [], "nothing was written at the walled label"


def test_the_same_label_is_accepted_for_a_credential_that_is_cleared(hub, box):
    box.push(VERB, memory_payload(label={"owner": "acme", "level": "confidential"}))
    assert drain(box, registry_publisher(Loopback("dana", hub)))["published"] == 1
    assert [i["created_by"] for i in items_for(hub, "dana")] == ["dana"]
    assert items_for(hub, "kofi") == [], "and the wall still hides it from the under-cleared"


def test_a_hub_refusal_of_the_body_comes_back_as_a_refusal_not_a_retry(hub, box):
    box.push(VERB, memory_payload(layer="episodic"))
    result = drain(box, registry_publisher(Loopback("kofi", hub)))
    assert (result["refused"], result["retryable"]) == (1, 0)


# --------------------------------------------------------------------------- #
# the conformance carve-out is pinned
# --------------------------------------------------------------------------- #


def test_the_conformance_carve_out_is_exactly_memory_create():
    """The scenarios do not exercise this verb (shared memory is not in the
    conformance core). That is a decision about one verb; this fails if it
    becomes a habit, or if the verb is ever put under the scenarios and the
    carve-out is left behind."""
    from agentco.conformance import CARRIES, OUTSIDE_CONFORMANCE

    assert OUTSIDE_CONFORMANCE == {VERB}
    assert set(CARRIES["outbox"]) == set(PUSH_VERBS) - {VERB}
