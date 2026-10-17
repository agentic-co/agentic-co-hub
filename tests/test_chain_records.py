"""Chain manifests, adoptions and approvals, over HTTP (ASOP v3.6, agentco/chains.py).

The tests worth having are the ones that would fail if the plane stored these
records without standing behind them:

  * **A manifest's digests are the store's.** A manifest pinning a version
    this store does not hold, or holding it at a digest this store did not
    compute, is refused — otherwise "pinned" means whatever the engine typed.
  * **Only a declared human adopts or approves, and the human is the
    signature.** An agent is refused; a body naming `adopted_by`, `impact` or
    `approved_by` is refused rather than ignored.
  * **The impact is computed, never typed.** It is the contract's
    `adoption_impact` of the two stored manifests.
  * **A run in a chain files where the chain's manifest pins it.** Not the
    active version, and never one the manifest does not pin
    (`adoption_required`) — until a person adopts a manifest that does.

Skipped as a module on a contract older than 0.6.0, where every route here
refuses `contract_unsupported` (which the last test asserts on any contract).
"""

from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from agentco import auth, chains
from agentco.errors import Refusal
from agentco.app import create_app

KEYS = {"dana": "dana-secret", "kofi": "kofi-secret", "operator": "op-secret"}
# Filing a step bead needs the park-clock fields too; a record alone does not.
DET = {"kind": "deterministic", "check": "make check", "max_park_seconds": 900, "on_timeout": "fail"}

needs_v36 = pytest.mark.skipif(not chains.SUPPORTED, reason="installed asop-spec predates v3.6 (0.6.0)")


@pytest.fixture()
def client(tmp_path):
    app = create_app(
        db_path=str(tmp_path / "api.sqlite3"),
        keys=KEYS,
        operator="operator",
        work_store=str(tmp_path / "work.jsonl"),
        sop_store=str(tmp_path / "sops.jsonl"),
        humans=["dana"],
    )
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


def get(client, path, actor, query=""):
    return client.get(f"{path}{query}", headers=signed("GET", path, actor, None))


def active_sop(client, title, **chain) -> dict:
    body = {"title": title, "inputs": [{"name": "source"}], "roles": {"worker": {"kind": "agent"}},
            "steps": [{"name": "do", "role": "worker", "purpose": title, "gate": DET}], **chain}
    created = post(client, "/sops", "dana", body)
    assert created.status_code == 200, created.text
    sop = created.json()["sop"]
    assert post(client, f"/sops/{sop['asop_id']}/activate", "dana", {"version": 1}).status_code == 200
    return sop


def digest(client, asop_id, version) -> str:
    return get(client, f"/sops/{asop_id}", "kofi", f"?version={version}").json()["digest"]


def manifest_for(client, chain, start, *members) -> dict:
    pins = [(start["asop_id"], start["version"])] + [(m["asop_id"], m["version"]) for m in members]
    return {
        "schema": "asop.chain-manifest/1",
        "chain": chain,
        "start": {"asop_id": start["asop_id"], "version": start["version"]},
        "members": sorted(({"asop_id": a, "version": v, "digest": digest(client, a, v)} for a, v in pins),
                          key=lambda m: m["asop_id"]),
        "components": [{"name": "checker", "version": "29", "digest": "sha256:" + "a" * 64}],
    }


@pytest.fixture()
def chain(client):
    """`intake` hands off to `extract`, both active at v1, and chain C1 pinned."""
    extract = active_sop(client, "Extract one source")
    intake = active_sop(client, "Take a source in", handoffs=[{
        "id": "extract", "to": {"asop_id": extract["asop_id"], "version": 1},
        "inputs": {"source": "$run.inputs.source"}}])
    manifest = manifest_for(client, "C1", intake, extract)
    recorded = post(client, "/chains/manifests", "kofi", {"manifest": manifest})
    assert recorded.status_code == 200, recorded.text
    return {"intake": intake, "extract": extract, "manifest": manifest, "receipt": recorded.json()}


# --------------------------------------------------------------------------- #
# manifests
# --------------------------------------------------------------------------- #


@needs_v36
def test_the_store_serves_a_digest_per_version(client):
    sop = active_sop(client, "Anything")
    first = digest(client, sop["asop_id"], 1)
    assert first.startswith("sha256:") and len(first) == 71
    assert digest(client, sop["asop_id"], 1) == first, "the digest is stable"
    post(client, f"/sops/{sop['asop_id']}/revise", "dana", {"purpose": "a different purpose"})
    assert digest(client, sop["asop_id"], 2) != first, "a changed procedure has a changed digest"


@needs_v36
def test_the_first_manifest_is_the_one_a_chain_starts_under(client, chain):
    receipt = chain["receipt"]
    assert receipt["state"] == "accepted" and receipt["effective"] is True
    view = get(client, "/chains/C1", "kofi").json()["chain"]
    assert view["startDigest"] == view["currentDigest"] == receipt["digest"]
    assert view["manifest"]["members"] == chain["manifest"]["members"]
    assert view["adoptions"] == []

    again = post(client, "/chains/manifests", "kofi", {"manifest": chain["manifest"]}).json()
    assert again["state"] == "unchanged" and again["digest"] == receipt["digest"]

    kinds = [e["kind"] for e in get(client, "/events", "kofi").json()["events"]]
    assert kinds.count("ChainManifestRecorded") == 1, "a repeat records nothing"


@needs_v36
def test_an_unknown_chain_reads_as_null(client):
    response = get(client, "/chains/nobody-pinned-this", "kofi")
    assert response.status_code == 200 and response.json()["chain"] is None


@needs_v36
def test_a_manifest_digest_the_store_did_not_compute_is_refused(client, chain):
    forged = json.loads(json.dumps(chain["manifest"]))
    forged["chain"] = "C2"
    forged["members"][0]["digest"] = "sha256:" + "0" * 64
    response = post(client, "/chains/manifests", "kofi", {"manifest": forged})
    assert response.status_code == 409
    body = response.json()
    assert body["code"] == "manifest_digest_mismatch" and body["remediation"]
    assert get(client, "/chains/C2", "kofi").json()["chain"] is None, "nothing was stored"


@needs_v36
def test_a_manifest_pinning_a_version_the_store_lacks_is_refused(client, chain):
    missing = json.loads(json.dumps(chain["manifest"]))
    missing["chain"] = "C3"
    for m in missing["members"]:
        if m["asop_id"] == chain["extract"]["asop_id"]:
            m["version"] = 9
    response = post(client, "/chains/manifests", "kofi", {"manifest": missing})
    assert response.status_code == 409 and response.json()["code"] == "manifest_member_unknown"


@needs_v36
def test_a_malformed_manifest_is_refused_with_the_contracts_reason(client):
    response = post(client, "/chains/manifests", "kofi", {"manifest": {"schema": "asop.chain-manifest/1"}})
    assert response.status_code == 400 and response.json()["code"] == "manifest_invalid"


# --------------------------------------------------------------------------- #
# adoption
# --------------------------------------------------------------------------- #


def _second_manifest(client, chain):
    """`extract` revised to v2 and activated, and a manifest pinning it."""
    extract_id = chain["extract"]["asop_id"]
    post(client, f"/sops/{extract_id}/revise", "dana", {"purpose": "extract, more carefully"})
    post(client, f"/sops/{extract_id}/activate", "dana", {"version": 2})
    new = manifest_for(client, "C1", chain["intake"], {"asop_id": extract_id, "version": 2})
    recorded = post(client, "/chains/manifests", "kofi", {"manifest": new}).json()
    assert recorded["state"] == "accepted" and recorded["effective"] is False, \
        "recording a manifest does not move the chain; adopting it does"
    return recorded["digest"]


@needs_v36
def test_the_impact_is_computed_from_the_two_manifests(client, chain):
    to = _second_manifest(client, chain)
    preview = get(client, "/chains/C1/impact", "kofi", f"?to={to}").json()
    by_name = {row["name"]: row for row in preview["impact"]}
    assert by_name[chain["extract"]["asop_id"]]["effect"] == "changed"
    assert by_name[chain["intake"]["asop_id"]]["effect"] == "unchanged"
    assert by_name["checker"]["effect"] == "unchanged"
    assert preview["from"] == chain["receipt"]["digest"] and preview["to"] == to


@needs_v36
def test_only_a_declared_human_adopts(client, chain):
    to = _second_manifest(client, chain)
    preview = get(client, "/chains/C1/impact", "kofi", f"?to={to}").json()["impact"]
    refused = post(client, "/chains/C1/adoptions", "kofi", {"to": to})
    assert refused.status_code == 403 and refused.json()["code"] == "capability_mismatch"
    assert get(client, "/chains/C1", "kofi").json()["chain"]["currentDigest"] == chain["receipt"]["digest"]

    adopted = post(client, "/chains/C1/adoptions", "dana", {"to": to, "reason": "extract v2 fixes the parser"})
    assert adopted.status_code == 200, adopted.text
    record = adopted.json()["adoption"]
    assert record["adopted_by"] == "dana" and record["schema"] == "asop.adoption/1"
    assert record["impact"] == preview, "the recorded impact is the computed one"
    view = get(client, "/chains/C1", "kofi").json()["chain"]
    assert view["currentDigest"] == to and view["startDigest"] == chain["receipt"]["digest"]
    assert [a["to"] for a in view["adoptions"]] == [to]


@pytest.mark.parametrize("field", ["impact", "adopted_by", "from"])
@needs_v36
def test_a_body_that_types_what_the_plane_computes_is_refused(client, chain, field):
    to = _second_manifest(client, chain)
    response = post(client, "/chains/C1/adoptions", "dana", {"to": to, field: "anything"})
    assert response.status_code == 400 and response.json()["code"] == "author_from_signature"


@needs_v36
def test_adopting_the_manifest_already_in_force_is_refused(client, chain):
    response = post(client, "/chains/C1/adoptions", "dana", {"to": chain["receipt"]["digest"]})
    assert response.status_code == 400 and response.json()["code"] == "adoption_invalid"


# --------------------------------------------------------------------------- #
# a run in a pinned chain
# --------------------------------------------------------------------------- #


def run_in_chain(client, asop_id, **extra):
    return post(client, f"/sops/{asop_id}/run", "dana", {
        "inputs": {"source": "doc.md"}, "bindings": {"worker": "kofi"}, "chain": "C1", **extra})


@needs_v36
def test_a_chain_run_files_at_the_pinned_version_not_the_active_one(client, chain):
    to = _second_manifest(client, chain)  # extract v2 is now ACTIVE, the chain still pins v1
    extract_id = chain["extract"]["asop_id"]

    run = run_in_chain(client, extract_id)
    assert run.status_code == 200, run.text
    assert run.json()["run"]["version"] == 1, "resolved from the pin, not from what is active now"

    refused = run_in_chain(client, extract_id, version=2)
    assert refused.status_code == 409 and refused.json()["code"] == "adoption_required"

    post(client, "/chains/C1/adoptions", "dana", {"to": to})
    after = run_in_chain(client, extract_id)
    assert after.status_code == 200 and after.json()["run"]["version"] == 2


@needs_v36
def test_a_chain_run_records_the_manifest_it_resolved_from(client, chain):
    run = run_in_chain(client, chain["intake"]["asop_id"]).json()["run"]
    assert run["chain"] == {"chain": "C1", "manifest": chain["receipt"]["digest"]}
    read = get(client, f"/runs/{run['runId']}", "kofi").json()["run"]
    assert read["chain"] == run["chain"]
    standalone = post(client, f"/sops/{chain['intake']['asop_id']}/run", "dana", {
        "inputs": {"source": "doc.md"}, "bindings": {"worker": "kofi"}}).json()["run"]
    assert standalone["chain"] is None


@needs_v36
def test_a_procedure_the_manifest_does_not_pin_is_refused_in_the_chain(client, chain):
    stranger = active_sop(client, "Not in the chain")
    response = run_in_chain(client, stranger["asop_id"])
    assert response.status_code == 409 and response.json()["code"] == "adoption_required"


@needs_v36
def test_a_filer_cannot_forge_the_chain_pin(client, chain):
    response = post(client, f"/sops/{chain['intake']['asop_id']}/run", "dana", {
        "inputs": {"source": "doc.md"}, "bindings": {"worker": "kofi"},
        "metadata": {"sop_chain": {"chain": "C1", "manifest": "sha256:" + "f" * 64}}})
    assert response.status_code >= 400 and response.json()["code"] == "metadata_reserved"


# --------------------------------------------------------------------------- #
# approvals
# --------------------------------------------------------------------------- #


@needs_v36
def test_a_person_approves_a_subset_at_a_digest(client):
    response = post(client, "/approvals", "dana",
                    {"artefact": "plan-approval", "digest": "sha256:aa", "subset": ["T1", "T3"]})
    assert response.status_code == 200, response.text
    record = response.json()["approval"]
    assert record == {"schema": "asop.approval/1", "artefact": "plan-approval", "digest": "sha256:aa",
                      "subset": ["T1", "T3"], "approved_by": "dana", "approved_at": record["approved_at"]}

    read = get(client, "/approvals", "kofi", "?artefact=plan-approval&digest=sha256:aa").json()
    assert read["current"] is True and read["latest"]["subset"] == ["T1", "T3"]
    stale = get(client, "/approvals", "kofi", "?artefact=plan-approval&digest=sha256:bb").json()
    assert stale["current"] is False, "an approval of an earlier artefact approved something else"
    assert "ArtefactApproved" in [e["kind"] for e in get(client, "/events", "kofi").json()["events"]]


@needs_v36
def test_an_agent_cannot_approve(client):
    response = post(client, "/approvals", "kofi", {"artefact": "plan-approval", "digest": "sha256:aa"})
    assert response.status_code == 403 and response.json()["code"] == "capability_mismatch"
    assert get(client, "/approvals", "kofi", "?artefact=plan-approval").json()["latest"] is None


@pytest.mark.parametrize("body", [
    {"artefact": "plan", "digest": ""},
    {"artefact": " plan", "digest": "sha256:aa"},
    {"artefact": "plan", "digest": "sha256:aa", "subset": ["T1", "T1"]},
    {"artefact": "plan", "digest": "sha256:aa", "subset": [True]},
    {"artefact": "plan", "digest": "sha256:aa", "subset": "T1"},
])
@needs_v36
def test_a_malformed_approval_is_refused(client, body):
    response = post(client, "/approvals", "dana", body)
    assert response.status_code == 400 and response.json()["code"] == "approval_invalid"


@needs_v36
def test_an_approval_naming_its_approver_is_refused(client):
    response = post(client, "/approvals", "dana",
                    {"artefact": "plan", "digest": "sha256:aa", "approved_by": "someone-else"})
    assert response.status_code == 400 and response.json()["code"] == "author_from_signature"


# --------------------------------------------------------------------------- #
# an older contract
# --------------------------------------------------------------------------- #


def test_an_older_contract_refuses_rather_than_storing_unvalidated(client, monkeypatch):
    monkeypatch.setattr(chains, "SUPPORTED", False)
    for response in (
        post(client, "/chains/manifests", "kofi", {"manifest": {}}),
        post(client, "/approvals", "dana", {"artefact": "plan", "digest": "sha256:aa"}),
        post(client, "/chains/C1/adoptions", "dana", {"to": "sha256:" + "a" * 64}),
    ):
        assert response.status_code == 501 and response.json()["code"] == "contract_unsupported"


# --------------------------------------------------------------------------- #
# the storage, on every registry backend
# --------------------------------------------------------------------------- #


@pytest.fixture()
def registry(backend, tmp_path, request):
    """The registry connection on the same backend the parametrised `library`
    uses. The HTTP tests above are SQLite-only; this is where `ON CONFLICT`,
    the adoption compare-and-set and `rowcount` meet Postgres."""
    from agentco import db

    if backend == "postgres":
        return db.connect(request.getfixturevalue("_pg_dsn"))
    return db.connect(tmp_path / "registry.sqlite3")


@needs_v36
def test_the_records_round_trip_on_every_backend(registry, library):
    def make(title, **chain):
        sop = library.create(title, inputs=[{"name": "source"}], roles={"worker": {"kind": "agent"}},
                             steps=[{"name": "do", "role": "worker", "purpose": title, "gate": DET}], **chain)
        return library.activate(sop.asop_id, 1, author_kind="human")

    extract = make("Extract one source")
    intake = make("Take a source in", handoffs=[{"id": "extract", "to": {"asop_id": extract.asop_id, "version": 1},
                                                 "inputs": {"source": "$run.inputs.source"}}])

    def manifest(extract_version):
        target = library.get(extract.asop_id, extract_version)
        return {"schema": "asop.chain-manifest/1", "chain": "C1",
                "start": {"asop_id": intake.asop_id, "version": 1},
                "members": sorted([{"asop_id": intake.asop_id, "version": 1, "digest": chains.store_digest(intake)},
                                   {"asop_id": extract.asop_id, "version": extract_version,
                                    "digest": chains.store_digest(target)}], key=lambda m: m["asop_id"])}

    first = chains.record_manifest(registry, library, actor="engine", doc=manifest(1))
    assert first["state"] == "accepted" and first["effective"]
    assert chains.record_manifest(registry, library, actor="engine", doc=manifest(1))["state"] == "unchanged"

    library.revise(extract.asop_id, purpose="more carefully", author_kind="human")
    library.activate(extract.asop_id, 2, author_kind="human")
    second = chains.record_manifest(registry, library, actor="engine", doc=manifest(2))
    assert second["effective"] is False

    assert chains.pin_for_run(registry, "C1", extract.asop_id, None)[0] == 1
    adopted = chains.adopt(registry, chain="C1", to=second["digest"], actor="dana", humans=["dana"])
    assert adopted["adoption"]["from"] == first["digest"]
    assert chains.pin_for_run(registry, "C1", extract.asop_id, None)[0] == 2
    view = chains.chain_view(registry, "C1")
    assert view["currentDigest"] == second["digest"] and len(view["adoptions"]) == 1
    assert len(view["manifests"]) == 2

    # Adopting the manifest already in force changes nothing, so it is not one.
    with pytest.raises(Refusal) as exc:
        chains.adopt(registry, chain="C1", to=second["digest"], actor="dana", humans=["dana"])
    assert exc.value.code == "adoption_invalid"

    # The compare-and-set: a second person who read the chain BEFORE the first
    # adoption, and adopts from what it ran under then, is refused rather than
    # forking the chain.
    real_current = chains._current
    chains._current = lambda conn, chain: first["digest"]
    try:
        with pytest.raises(Refusal) as exc:
            chains.adopt(registry, chain="C1", to=second["digest"], actor="dana", humans=["dana"])
    finally:
        chains._current = real_current
    assert exc.value.code == "work_conflict"
    assert chains.chain_view(registry, "C1")["currentDigest"] == second["digest"]
    assert len(chains.chain_view(registry, "C1")["adoptions"]) == 1

    chains.record_approval(registry, actor="dana", humans=["dana"], artefact="plan", digest="sha256:aa")
    chains.record_approval(registry, actor="dana", humans=["dana"], artefact="plan", digest="sha256:bb",
                           subset=[1, "T2"])
    read = chains.approvals_of(registry, "plan", "sha256:bb")
    assert [a["digest"] for a in read["history"]] == ["sha256:aa", "sha256:bb"]
    assert read["current"] is True and read["latest"]["subset"] == [1, "T2"]
