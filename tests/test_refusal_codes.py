"""Renamed refusal codes (owner decision 9, 2026-10-18).

* `human_required` — a non-human actor attempted a human-only verb. Formerly
  `capability_mismatch`, which now means only a capability or role binding failure.
* `manifest_unknown` — the manifest a request names is not recorded. Formerly
  `manifest_member_unknown`, which now means only an unknown or draft member.

For one minor release the payload of a renamed refusal carries
`aliases: [<old code>]` (deprecated), so a client branching on the old code keeps
working. Asserted over the real HTTP routes and a live socket; and a test fails if
the installed contract catalogs these codes under different spellings.
"""

from __future__ import annotations

import difflib
import importlib.metadata
import json
import time

import pytest

from agentco import chains
from agentco.errors import HUMAN_REQUIRED, MANIFEST_UNKNOWN
from tests.test_chain_records import _second_manifest, chain, client, get, post  # noqa: F401

needs_v36 = pytest.mark.skipif(not chains.SUPPORTED, reason="chain records need asop-spec 0.6.0")


def assert_renamed(response, code, alias):
    body = response.json()
    assert body["code"] == code, body
    assert body["aliases"] == [alias], "the old code rides along for one minor"


# --------------------------------------------------------------------------- #
# human_required, verb by verb
# --------------------------------------------------------------------------- #


@needs_v36
def test_adoption_by_an_agent_is_human_required(client, chain):
    to = _second_manifest(client, chain)
    response = post(client, "/chains/C1/adoptions", "kofi", {"to": to})
    assert response.status_code == 403
    assert_renamed(response, HUMAN_REQUIRED, "capability_mismatch")


@needs_v36
def test_an_approval_by_an_agent_is_human_required(client):
    response = post(client, "/approvals", "kofi", {"artefact": "plan", "digest": "sha256:aa"})
    assert response.status_code == 403
    assert_renamed(response, HUMAN_REQUIRED, "capability_mismatch")


@needs_v36
def test_the_target_manifest_not_recorded_is_manifest_unknown(client, chain):
    response = get(client, "/chains/C1/impact", "kofi", "?to=sha256:" + "e" * 64)
    assert response.status_code == 404
    assert_renamed(response, MANIFEST_UNKNOWN, "manifest_member_unknown")


@needs_v36
def test_an_unknown_member_keeps_manifest_member_unknown_without_an_alias(client, chain):
    missing = json.loads(json.dumps(chain["manifest"]))
    missing["chain"] = "C9"
    for member in missing["members"]:  # the non-start member: members sort by random id
        if member["asop_id"] == chain["extract"]["asop_id"]:
            member["version"] = 9
    body = post(client, "/chains/manifests", "kofi", {"manifest": missing}).json()
    assert body["code"] == "manifest_member_unknown" and "aliases" not in body


def memory_client(tmp_path):
    from fastapi.testclient import TestClient

    from agentco.app import create_app
    from tests.test_memory_store import KEYS, LEVELS

    return TestClient(create_app(
        db_path=str(tmp_path / "api.sqlite3"), keys=KEYS, operator="dana", humans=["dana"],
        work_store=str(tmp_path / "work.jsonl"), sop_store=str(tmp_path / "sops.jsonl"),
        memory_store=str(tmp_path / "memory.jsonl"), hub_id="hub-a", memory_levels=LEVELS,
        hub_clearance={"acme": "confidential"}, clearances={"kofi": {"acme": "internal"}},
        memory_enabled=True))


def test_memory_human_only_verbs_keep_their_existing_code(tmp_path):
    """Spec 0.6.1 §10's table moves only chain_adopt, approve and run_transfer to
    `human_required` ("No existing code changes"): the hub's memory verbs keep
    `capability_mismatch`, with no alias."""
    from tests.test_memory_store import http_lesson, post as mpost

    client = memory_client(tmp_path)
    item = http_lesson(client).json()["item"]["item_id"]
    for path, body in ((f"/memory/{item}/pin", {"review_by": "2027-01-01"}), (f"/memory/{item}/retire", {}),
                       (f"/memory/{item}/rate", {"rating": "up"}), ("/memory/replay", {})):
        response = mpost(client, path, "kofi", body).json()
        assert response["code"] == "capability_mismatch" and "aliases" not in response, path


def test_the_run_transfer_codes_follow_the_spec(tmp_path):
    from fastapi.testclient import TestClient

    from tests.test_run_ownership import http, http_runtime_run, make_app

    client = TestClient(make_app(tmp_path))
    run = http_runtime_run(client)
    path, body = f"/runs/{run['runId']}/transfer", {"to": "hub-a", "reason": "gone"}
    assert_renamed(http(client, "POST", path, "kofi", body), HUMAN_REQUIRED, "capability_mismatch")
    assert_renamed(http(client, "POST", path, "eve", body), "transfer_refused:administrator", "capability_mismatch")
    assert_renamed(http(client, "POST", path, "dana", {"to": "hub-a"}), "transfer_refused:reason", "transfer_invalid")
    assert_renamed(http(client, "POST", path, "dana", {"to": "runtime-a", "reason": "noop"}),
                   "transfer_refused:same_owner", "transfer_invalid")
    # Who is asking comes first (§10): an agent learns nothing about its body.
    assert http(client, "POST", path, "kofi", {"to": "hub-a"}).json()["code"] == HUMAN_REQUIRED
    # Then the record's validity, before the administrator rule.
    assert http(client, "POST", path, "eve", {"to": "", "reason": "x"}).json()["code"] == "transfer_refused:malformed"


@needs_v36
def test_adoption_checks_the_person_before_the_body(client, chain):
    to = _second_manifest(client, chain)
    body = post(client, "/chains/C1/adoptions", "kofi", {"to": to, "impact": []}).json()
    assert body["code"] == HUMAN_REQUIRED, "not author_from_signature: the caller is settled first"


def test_the_administrator_registry_reads_the_standard_name_first(monkeypatch):
    from agentco.ownership import admins_from_env

    monkeypatch.setenv("AGENTCO_ADMINS", "legacy-admin")
    monkeypatch.delenv("ASOP_ADMINISTRATORS", raising=False)
    assert admins_from_env() == {"legacy-admin"}, "the deprecated name is the fallback"
    monkeypatch.setenv("ASOP_ADMINISTRATORS", "standard-admin")
    assert admins_from_env() == {"standard-admin"}, "both set: the standard name wins, the legacy one is ignored"
    monkeypatch.setenv("ASOP_ADMINISTRATORS", "")
    assert admins_from_env() == frozenset(), "declared empty means nobody, not the fallback"


def test_a_real_capability_failure_keeps_capability_mismatch():
    """A claim or verdict by a node lacking a declared capability is a binding
    failure, not a human-only verb: unchanged, and no alias."""
    from agentco.refusals import classify
    from agentco.work import CapabilityError

    refusal = classify(CapabilityError("lacks billing-erp")).to_dict()
    assert refusal["code"] == "capability_mismatch" and "aliases" not in refusal


# --------------------------------------------------------------------------- #
# a live socket
# --------------------------------------------------------------------------- #


def test_live_wire_the_alias_crosses_a_real_socket(tmp_path):
    import socket
    import threading

    import uvicorn
    from fastapi.testclient import TestClient

    from agentco.publish import Registry, RegistryError
    from tests.test_run_ownership import KEYS, http_runtime_run, make_app

    app = make_app(tmp_path)
    run = http_runtime_run(TestClient(app))
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    try:
        url = f"http://127.0.0.1:{port}"
        for actor, body, status, code, alias in (
            ("kofi", {"to": "hub-a", "reason": "gone"}, 403, "human_required", "capability_mismatch"),
            ("eve", {"to": "hub-a", "reason": "gone"}, 403, "transfer_refused:administrator", "capability_mismatch"),
            ("dana", {"to": "hub-a"}, 400, "transfer_refused:reason", "transfer_invalid"),
            ("dana", {"to": "runtime-a", "reason": "noop"}, 400, "transfer_refused:same_owner", "transfer_invalid"),
        ):
            with pytest.raises(RegistryError) as exc:
                Registry(actor, KEYS[actor], url)._call("POST", f"/runs/{run['runId']}/transfer", body)
            assert exc.value.status == status, (actor, body)
            assert exc.value.payload["code"] == code and exc.value.payload["aliases"] == [alias]
    finally:
        server.should_exit = True
        thread.join(timeout=5)


# --------------------------------------------------------------------------- #
# the hub and the installed contract agree on the strings
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("code", [HUMAN_REQUIRED, MANIFEST_UNKNOWN])
def test_the_hub_and_the_installed_contract_agree_on_the_code(code):
    """Skipped until the installed asop-spec catalogs the code (0.6.1). Once it
    does, the strings must match exactly — and a near-miss spelling in the
    catalog fails even before then, because that is a disagreement."""
    from asop.refusals import CODES

    catalogued = set(CODES)
    near = [c for c in difflib.get_close_matches(code, sorted(catalogued), n=3, cutoff=0.85) if c != code]
    assert not near or code in catalogued, f"the contract spells {code!r} as {near}"
    if code not in catalogued:
        version = importlib.metadata.version("asop-spec")
        pytest.skip(f"asop-spec {version} does not catalog {code!r} yet")
    assert code in catalogued


def test_the_hub_renames_match_the_contracts_legacy_uses():
    """Every (new, old) pair the hub aliases is one the spec lists in LEGACY_USES,
    and every transfer rule the hub raises is one of the spec's TRANSFER_RULES."""
    import asop.refusals as refusals

    from agentco.errors import RENAMED

    legacy = getattr(refusals, "LEGACY_USES", None)
    if legacy is None:
        pytest.skip(f"asop-spec {importlib.metadata.version('asop-spec')} has no LEGACY_USES yet")
    pairs = {(old, new) for (old, _situation), new in legacy.items()}
    for new, old in RENAMED:
        assert (old, new) in pairs, f"the spec does not list {old!r} -> {new!r} as a legacy use"
    from asop.ownership import TRANSFER_RULES

    for new, _ in RENAMED:
        if new.startswith("transfer_refused:"):
            assert new.split(":", 1)[1] in TRANSFER_RULES, new
    assert "transfer_refused:<rule>" in refusals.CODES
