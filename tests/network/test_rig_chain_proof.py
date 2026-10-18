"""The live chain proof, automated: an ASOP v3.6 chain through a real hub,
across a restart, on SQLite and on Postgres.

This is the proof first run by hand against the image, turned into a scenario
so it regresses on its own: the same 22 checks (the chain, D2, and the D6 and
D8 wire checks). Three
SOPs, authored over HTTP: A frames the source, hands off to B; B builds and
validates, hands off to C; C reviews, with a rework edge back to B. The run
crosses a container restart in the middle, and the second half asserts nothing
was lost.

D2 was decided after the hand-run (owner decision 2026-10-18, option A), so the
check that run recorded as KNOWN now asserts the decided behaviour: a
`cannot_evaluate` result is not charged, its decision is `re-run`, and the step
is worked again under a new fence.

Checks are numbered by the proof's sections (1 authoring, 2 manifests and
approvals, 3 running, 5 outcomes, 6 restart, 8 wire). Every outcome is
recorded; the test fails listing every check that failed, not just the first.
`AGENTCO_RIG_REPORT=<dir>` also writes them as JSON evidence.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

DET = {"kind": "deterministic", "check": "make check", "max_park_seconds": 900, "on_timeout": "fail"}
SUITE = {"kind": "deterministic", "check": "make test", "max_park_seconds": 900, "on_timeout": "fail",
         "cannot_evaluate_exit": 3, "min_cases": 1}
READY = {"kind": "deterministic", "check": "check-source --final", "cannot_evaluate_exit": 3}
ROLES = {"implementer": {"kind": "agent"}, "validator": {"kind": "agent"}, "owner": {"kind": "human"}}
BIND = {"implementer": "kofi", "validator": "ana", "owner": "operator"}
PARK = 5  # seconds the human question waits before proceed_on_default may fire


def question(verifier, park):
    return {"kind": "human", "check": "which retention period applies?", "verifier": verifier,
            "max_park_seconds": park, "on_timeout": "proceed_on_default",
            "default": {"option": "keep the current period", "consequence": "the purge job is built for it",
                        "audience": "product owner"}}


def c_body(b_id):
    return {"title": "C: review the build", "purpose": "review", "inputs": [{"name": "source"}],
            "roles": ROLES, "constraints": [{"distinct": ["implementer", "validator"]}],
            "outcomes": ["ACCEPTED", "FINDINGS"], "outputs": [{"name": "findings"}],
            "steps": [{"name": "review", "role": "validator", "purpose": "review the build", "gate": DET}],
            "rework": [{"id": "rebuild", "to": {"asop_id": b_id, "versions": ">=1"}, "on_outcomes": ["FINDINGS"],
                        "findings": "findings", "max_rounds": 2, "consumes_attempt": False, "tier_bump": True}]}


def b_body(c_id=None):
    b = {"title": "B: build", "purpose": "build", "inputs": [{"name": "source"}],
         "roles": ROLES, "constraints": [{"distinct": ["implementer", "validator"]}],
         "entry_gates": [{"id": "source-buildable", "over": ["source"], "gate": READY}],
         "outcomes": ["BUILT"],
         "steps": [{"name": "build", "role": "implementer", "purpose": "build it", "gate": SUITE},
                   {"name": "validate", "role": "validator", "purpose": "validate it", "gate": DET, "after": [1]}]}
    if c_id:
        b["handoffs"] = [{"id": "review", "to": {"asop_id": c_id, "version": 1}, "on_outcomes": ["BUILT"],
                          "preflight": True, "inputs": {"source": "$run.inputs.source"}}]
    return b


def a_body(b_id, b_version, park):
    return {"title": "A: frame the source", "purpose": "frame", "inputs": [{"name": "source"}],
            "roles": ROLES, "constraints": [{"distinct": ["implementer", "validator"]}],
            "entry_gates": [{"id": "source-ready", "over": ["source"], "gate": READY}],
            "outcomes": ["FRAMED"],
            "steps": [{"name": "frame", "role": "implementer", "purpose": "frame the work", "gate": DET},
                      {"name": "retention", "role": "owner", "purpose": "a person decides retention",
                       "gate": question("dana", park), "after": [1]}],
            "handoffs": [{"id": "build", "to": {"asop_id": b_id, "version": b_version}, "on_outcomes": ["FRAMED"],
                          "inputs": {"source": "$run.inputs.source"}}]}


def det(code=0):
    return {"check": "make check", "exit_status": code, "environment": "ci/synthetic",
            "at": "2026-10-18T00:00:00+00:00"}


def suite(code=0, cases=5):
    return {"check": "make test", "exit_status": code, "environment": "ci/synthetic",
            "at": "2026-10-18T00:00:00+00:00", "cases_run": cases}


def brief(status, payload):
    if isinstance(payload, dict) and payload.get("code"):
        return f"HTTP {status} {payload['code']}"
    return f"HTTP {status}"


class ChainProof:
    def __init__(self, rig, hub):
        self.rig, self.hub, self.results, self.st = rig, hub, [], {}

    def call(self, actor, method, path, body=None, query=""):
        return self.rig.call(self.hub, actor, method, path, body, query=query)

    def check(self, n, name, ok, detail):
        self.results.append({"check": n, "name": name, "pass": bool(ok), "detail": detail})

    def pull(self, actor, ttl=600):
        return self.call(actor, "POST", "/work/pull", {"ttlSeconds": ttl})

    def report(self, actor, item_id, attempt, status="done", attestation=None):
        body = {"status": status, "attempt": attempt}
        if attestation is not None:
            body["attestation"] = attestation
        return self.call(actor, "POST", f"/work/{item_id}/report", body)

    def item(self, item_id):
        _, page = self.call("dana", "GET", "/work", query="limit=500")
        return next((i for i in page.get("items", []) if i["id"] == item_id), None)

    def run_in_chain(self, asop_id, bindings, version=None):
        body = {"inputs": {"source": "docs/requirements.md"}, "bindings": bindings, "chain": "C1"}
        if version is not None:
            body["version"] = version
        return self.call("dana", "POST", f"/sops/{asop_id}/run", body)

    def manifest(self, versions):
        digests = {i: self.call("kofi", "GET", f"/sops/{i}", query=f"version={v}")[1]["digest"]
                   for i, v in versions.items()}
        return {"schema": "asop.chain-manifest/1", "chain": "C1",
                "start": {"asop_id": self.st["a"], "version": 1},
                "members": sorted(({"asop_id": i, "version": versions[i], "digest": d} for i, d in digests.items()),
                                  key=lambda m: m["asop_id"]),
                "components": [{"name": "checker", "version": "29", "digest": "sha256:" + "a" * 64}]}

    # ------------------------------------------------------------------ before

    def before(self):
        check, call = self.check, self.call
        # (1) author: B v1 -> C v1 (rework -> B) -> B v2 (hand-off -> C) -> A v1 (hand-off -> B v2)
        s, b = call("dana", "POST", "/sops", b_body())
        b_id = b["sop"]["asop_id"]
        s2, c = call("dana", "POST", "/sops", c_body(b_id))
        c_id = c["sop"]["asop_id"]
        s3, _ = call("dana", "POST", f"/sops/{b_id}/revise", {"handoffs": b_body(c_id)["handoffs"]})
        s4, a = call("dana", "POST", "/sops", a_body(b_id, 2, PARK))
        a_id = a["sop"]["asop_id"]
        self.st.update(a=a_id, b=b_id, c=c_id)
        acts = [call("dana", "POST", f"/sops/{i}/activate", {"version": v})[0]
                for i, v in ((b_id, 2), (c_id, 1), (a_id, 1))]
        a_read = call("kofi", "GET", f"/sops/{a_id}")[1]["sop"]
        b_read = call("kofi", "GET", f"/sops/{b_id}")[1]["sop"]
        c_read = call("kofi", "GET", f"/sops/{c_id}")[1]["sop"]
        stored = (a_read["entry_gates"][0]["gate"]["cannot_evaluate_exit"] == 3
                  and a_read["steps"][1]["gate"]["on_timeout"] == "proceed_on_default"
                  and b_read["entry_gates"][0]["id"] == "source-buildable"
                  and b_read["handoffs"][0]["preflight"] is True
                  and c_read["rework"][0] == c_body(b_id)["rework"][0])
        check(1, "author A/B/C v3.6 chain, all fields round-trip", all(x == 200 for x in (s, s2, s3, s4, *acts)) and stored,
              f"create {s},{s2}; revise B {s3}; create A {s4}; activate {acts}; v3.6 fields stored={stored}")

        # (2) manifest with the hub's digests; a forged one refused; approval
        manifest = self.manifest({a_id: 1, b_id: 2, c_id: 1})
        bad = json.loads(json.dumps(manifest))
        bad["chain"] = "C-forged"
        bad["members"][0]["digest"] = "sha256:" + "0" * 64
        sb, pb = call("kofi", "POST", "/chains/manifests", {"manifest": bad})
        sm, pm = call("kofi", "POST", "/chains/manifests", {"manifest": manifest})
        check(2, "manifest recorded with the hub's digests; a forged digest refused",
              sm == 200 and pm.get("effective") and sb == 409,
              f"manifest {brief(sm, pm)} effective={pm.get('effective')}; forged {brief(sb, pb)}")
        self.st["manifest"] = pm["digest"]
        sa, pa = call("kofi", "POST", "/approvals", {"artefact": "task-list", "digest": "sha256:t1", "subset": ["T1", "T3"]})
        sh, ph = call("dana", "POST", "/approvals", {"artefact": "task-list", "digest": "sha256:t1", "subset": ["T1", "T3"]})
        cur = call("ana", "GET", "/approvals", query="artefact=task-list&digest=sha256:t1")[1]
        stale = call("ana", "GET", "/approvals", query="artefact=task-list&digest=sha256:t2")[1]
        check(2, "approval by the declared human, bound to digest + subset; agent refused",
              sa == 403 and sh == 200 and cur["current"] and not stale["current"],
              f"agent {brief(sa, pa)}; human {brief(sh, ph)} subset={ph.get('approval', {}).get('subset')}; "
              f"current@t1={cur['current']} current@t2={stale['current']}")

        # (3) run A in the chain; distinct roles; a person may not verify their own step
        self_sign = self.run_in_chain(a_id, {**BIND, "owner": "dana"})
        check(3, "human step bound to its own gate's verifier is refused",
              self_sign[0] == 422 and self_sign[1].get("code") == "gate_invalid", brief(*self_sign))
        sr, ra = self.run_in_chain(a_id, BIND)
        run_a = ra["run"]
        same = self.run_in_chain(a_id, {"implementer": "kofi", "validator": "kofi", "owner": "operator"})
        check(3, "run A filed in chain C1 at the pinned version; same actor in both distinct roles refused",
              sr == 200 and run_a["chain"]["manifest"] == pm["digest"] and same[0] >= 400,
              f"run {brief(sr, ra)} v{run_a['version']} chain={run_a['chain']['chain']}; same-actor {brief(*same)}")
        s1, p1 = self.pull("kofi")
        rep = self.report("kofi", p1["item"]["id"], p1["attempt"], attestation=det(0))
        check(3, "A step 1 (kofi): deterministic gate passes -> done",
              rep[0] == 200 and rep[1]["item"]["status"] == "done",
              f"pull {brief(s1, p1)}; report {brief(*rep)} -> {rep[1].get('item', {}).get('status')}")
        _, p2 = self.pull("operator")
        step2 = p2["item"]
        rep2 = self.report("operator", step2["id"], p2["attempt"])
        check(3, "A step 2 (operator executes; dana verifies; proceed_on_default): parks awaiting_verify",
              rep2[0] == 200 and rep2[1]["item"]["status"] == "awaiting_verify",
              f"report {brief(*rep2)} -> {rep2[1].get('item', {}).get('status')}")
        check(3, "entry gates: stored and served, NOT evaluated by the hub at filing (runtime duty)",
              True, "run A filed with no source-readiness evaluation; the gate is in GET /sops for a runtime to run")

        # run B (the hand-off target): this test acts as the runtime that files it
        _, rb = self.run_in_chain(b_id, BIND)
        run_b = rb["run"]
        build_id, validate_id = run_b["steps"][0]["itemId"], run_b["steps"][1]["itemId"]
        _, pp1 = self.pull("kofi", ttl=2)
        time.sleep(3)
        _, pp2 = self.pull("kofi", ttl=600)
        stale_r = self.report("kofi", build_id, pp1["attempt"], attestation=suite(0))
        check(3, "stale fence refused (lease expired, re-pulled at a higher attempt)",
              pp2["item"]["id"] == build_id and pp2["attempt"] > pp1["attempt"] and stale_r[0] in (409, 400, 403),
              f"attempt {pp1['attempt']} -> {pp2['attempt']}; report on old attempt {brief(*stale_r)}")
        ce = self.report("kofi", build_id, pp2["attempt"], attestation=suite(3, cases=0))
        ce_item = self.item(build_id)
        blocked = self.pull("ana")
        check(3, "cannot_evaluate (exit 3) does NOT release dependents",
              blocked[1].get("state") == "empty" and ce_item["status"] != "done",
              f"report {brief(*ce)} -> {ce_item['status']}; ana's pull {blocked[1].get('state')}")
        meta = ce_item.get("metadata") or {}
        flag = meta.get("verify_cannot_evaluate") or {}
        check(3, "D2 (decided, option A): cannot_evaluate is not charged, decision re-run, flagged pending",
              ce_item["verify_failures"] == 0 and meta.get("verify_retry", {}).get("decision") == "re-run"
              and flag.get("pending") is True,
              f"verify_failures={ce_item['verify_failures']} status={ce_item['status']} "
              f"decision={meta.get('verify_retry', {}).get('decision')} pending={flag.get('pending')}")

        ev = call("dana", "GET", "/events", query="limit=500")[1]
        self.st.update(run_a=run_a["runId"], run_b=run_b["runId"], a_step2=step2["id"], build=build_id,
                       validate=validate_id, build_attempt=ce_item["lease_attempt"], build_status=ce_item["status"],
                       build_failures=ce_item["verify_failures"], cursor=ev["nextCursor"], parked_at=time.time())

    # ------------------------------------------------------------------- after

    def after(self):
        check, call, st = self.check, self.call, self.st
        # (6) restart: nothing lost
        chain = call("ana", "GET", "/chains/C1")[1]
        appr = call("ana", "GET", "/approvals", query="artefact=task-list&digest=sha256:t1")[1]
        ra = call("ana", "GET", f"/runs/{st['run_a']}")[1]
        rb = call("ana", "GET", f"/runs/{st['run_b']}")[1]
        since = call("ana", "GET", "/events", query=f"since={st['cursor']}")[1]
        b = self.item(st["build"])
        ok = (chain["chain"]["currentDigest"] == st["manifest"] and appr["current"]
              and ra["run"]["chain"]["manifest"] == st["manifest"] and rb["run"]["steps"][0]["status"] == b["status"]
              and b["status"] == st["build_status"] and b["verify_failures"] == st["build_failures"]
              and b["lease_attempt"] == st["build_attempt"] and since["events"] == [])
        check(6, "after restart: manifest, approval, runs, step status, fence and feed cursor all intact", ok,
              f"build status={b['status']} (saved {st['build_status']}) attempt={b['lease_attempt']} "
              f"(saved {st['build_attempt']}) failures={b['verify_failures']}; events since cursor={len(since['events'])}")

        # proceed_on_default: let the park clock run out, then the pulse sweeps, in the container
        wait = PARK + 1 - (time.time() - st["parked_at"])
        if wait > 0:
            time.sleep(wait)
        pulse = self.rig.exec(self.hub, "python3", "-m", "agentco", "pulse", "--apply")
        q = self.item(st["a_step2"])
        check(3, "human gate times out with proceed_on_default -> done on its declared default",
              q["status"] == "done",
              f"pulse exit {pulse.returncode}; step status={q['status']}; resolution="
              f"{json.dumps((q.get('metadata') or {}).get('verify_resolution', {}))[:160]}")

        # finish B: the re-run under a new fence passes, then the validator (a distinct actor)
        rp = self.pull("kofi")
        rerun = rp[1].get("item", {}).get("id") == st["build"] and rp[1]["attempt"] > st["build_attempt"]
        rr = self.report("kofi", st["build"], rp[1]["attempt"], attestation=suite(0, cases=5)) if rerun else (0, {})
        pv = self.pull("ana")
        rv = self.report("ana", st["validate"], pv[1]["attempt"], attestation=det(0)) if pv[1].get("item") else (0, {})
        check(3, "B: the re-run (new fence) passes and releases validate; ana (validator) completes it",
              rerun and rr[0] == 200 and pv[1].get("item", {}).get("id") == st["validate"] and rv[0] == 200,
              f"re-pull new fence={rerun}; report {brief(*rr)} -> {rr[1].get('item', {}).get('status')}; "
              f"ana pull validate={pv[1].get('item', {}).get('id') == st['validate']}; report {brief(*rv)}")

        # adoption_required, then adoption by the human
        a_id, b_id, c_id = st["a"], st["b"], st["c"]
        call("dana", "POST", f"/sops/{b_id}/revise", {"purpose": "build, with the parser fix"})
        call("dana", "POST", f"/sops/{b_id}/activate", {"version": 3})
        refused = self.run_in_chain(b_id, BIND, version=3)
        unpinned = self.run_in_chain(b_id, BIND)
        check(3, "a run asking for B v3 while the manifest pins v2 is refused adoption_required; unversioned files at v2",
              refused[0] == 409 and refused[1].get("code") == "adoption_required" and unpinned[1]["run"]["version"] == 2,
              f"v3 {brief(*refused)}; no version -> v{unpinned[1]['run']['version']}")
        rec = call("kofi", "POST", "/chains/manifests", {"manifest": self.manifest({a_id: 1, b_id: 3, c_id: 1})})[1]
        imp = call("kofi", "GET", "/chains/C1/impact", query=f"to={rec['digest']}")[1]
        ag = call("kofi", "POST", "/chains/C1/adoptions", {"to": rec["digest"]})
        hu = call("dana", "POST", "/chains/C1/adoptions", {"to": rec["digest"], "reason": "B v3 fixes the parser"})
        now_v3 = self.run_in_chain(b_id, BIND)
        effects = {r["name"]: r["effect"] for r in imp["impact"]}
        check(2, "adoption: impact computed, agent refused, human records it, chain runs B v3 after",
              ag[0] == 403 and hu[0] == 200 and effects.get(b_id) == "changed" and now_v3[1]["run"]["version"] == 3,
              f"impact B={effects.get(b_id)} A={effects.get(a_id)}; agent {brief(*ag)}; human {brief(*hu)}; "
              f"B now files at v{now_v3[1]['run']['version']}")

        # C: a failing gate, repeated: the hub's repair budget (fix -> escalate -> stop)
        review = self.run_in_chain(c_id, BIND)[1]["run"]["steps"][0]["itemId"]
        pc = self.pull("ana")
        f1 = self.report("ana", review, pc[1]["attempt"], attestation=det(1))
        f2 = call("ana", "POST", f"/work/{review}/attest", {"attestation": det(1)})
        f3 = call("ana", "POST", f"/work/{review}/attest", {"attestation": det(1)})
        rv_item = self.item(review)
        decision = (rv_item.get("metadata") or {}).get("verify_retry", {}).get("decision")
        check(3, "a failing gate within the bound records fix, then escalate, then stop (the hub's repair budget)",
              f1[0] == 200 and rv_item["verify_failures"] == 3 and decision == "stop",
              f"report {brief(*f1)}; attest x2 {f2[0]},{f3[0]}; failures={rv_item['verify_failures']} last={decision}")
        rounds = [self.run_in_chain(b_id, BIND)[0] for _ in range(3)]
        check(3, "rework edge C->B (max_rounds 2): NOT executed or bounded by the hub (runtime duty)",
              rounds == [200, 200, 200], f"three further B runs filed in C1 by the caller: {rounds}")

        # (5) outcomes per version and per step
        so, out = call("ana", "GET", f"/sops/{b_id}/outcomes")
        by_v = {r["version"]: r for r in out.get("outcomes", {})}
        check(5, "sop_outcomes for B: separate rows for v2 (done) and v3 (in flight), each with per-step rows",
              so == 200 and by_v.get(2, {}).get("runs", 0) >= 1 and by_v.get(2, {}).get("done", 0) >= 1
              and by_v.get(3, {}).get("runs", 0) >= 1 and [x["name"] for x in by_v[2]["steps"]] == ["build", "validate"],
              "; ".join(f"v{v}: runs={r['runs']} done={r['done']} inFlight={r['inFlight']}" for v, r in sorted(by_v.items())))
        so_c, out_c = call("ana", "GET", f"/sops/{c_id}/outcomes")
        step_c = out_c["outcomes"][0]["steps"][0]
        check(5, "sop_outcomes for C: the failing review counted as verifyFailed at step level",
              so_c == 200 and step_c["verifyFailed"] == 1, f"{brief(so_c, out_c)} verifyFailed={step_c['verifyFailed']}")

        # (8) D6 / D8 over the real socket: snapshot ttlDays over HTTP and the remote MCP relay;
        #     the events kind filter over the relay
        from agentco.mcp_server import create_server

        def days_left(receipt):
            return (datetime.fromisoformat(receipt["expiresAt"]) - datetime.now(timezone.utc)).total_seconds() / 86400

        sh, snap = call("kofi", "POST", "/snapshots", {"artifactUri": "file:///nonexistent/d6.md", "purpose": "probe",
                                                       "ttlDays": 2})
        relay = create_server(registry=self.rig.registry(self.hub, "kofi"))
        tool = lambda name: relay._tool_manager.get_tool(name).fn  # noqa: E731
        rsnap = tool("snapshot")(artifact_uri="file:///nonexistent/d6-relay.md", purpose="probe", ttl_days=3)
        check(8, "D6 snapshot ttlDays honoured over HTTP (2d) and the remote MCP relay (3d)",
              sh == 200 and 1.9 < days_left(snap) < 2.1 and 2.9 < days_left(rsnap) < 3.1,
              f"HTTP {sh}; relay receipt expiresAt present={'expiresAt' in rsnap}")
        page = tool("events")(kind="ChainAdopted")
        kinds = {e["kind"] for e in page["events"]}
        check(8, "D8 events kind filter honoured over the remote MCP relay",
              kinds == {"ChainAdopted"}, f"relay events(kind=ChainAdopted) -> {sorted(kinds)} ({len(page['events'])})")


@pytest.mark.parametrize("hub", ["chain-sqlite", "chain-pg"])
def test_the_live_chain_proof(rig, hub):
    proof = ChainProof(rig, hub)
    backend = rig.exec(hub, "sh", "-c", 'echo "${AGENTCO_DB%%:*}"').stdout.strip()
    assert backend == ("postgresql" if hub == "chain-pg" else "/var/lib/agentco/hub.sqlite3"), backend
    proof.before()
    rig.restart(hub)
    proof.after()
    report = os.environ.get("AGENTCO_RIG_REPORT")
    if report:
        Path(report).mkdir(parents=True, exist_ok=True)
        (Path(report) / f"chain-proof-{hub}.json").write_text(json.dumps(proof.results, indent=1), encoding="utf-8")
    failed = [f"[{r['check']}] {r['name']} | {r['detail']}" for r in proof.results if not r["pass"]]
    assert len(proof.results) == 22, f"the proof ran {len(proof.results)} checks, not 22"
    assert not failed, "\n".join(failed)
