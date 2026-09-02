"""Conformance — one semantic core, three transports, identical results.

The plane has one meaning and three ways to reach it: the HTTP surface, the MCP
tool surface, and the outbox a drainer relays. Two write paths were accepted in
`docs/decisions/0002-participation-ladder.md` with one condition: they must stay
semantically identical, and a suite — not a habit — is what makes that
checkable. This module is that suite.

The method is deliberately dumb. A **scenario** is a script of steps — an actor,
a verb, arguments — with no transport in it. It is run once with every step
performed by the in-process core (the queue, the library, the registry
functions), and once per transport with every step that transport *carries*
performed through it and the rest through the core. After each run the world is
photographed: every work item, every procedure version, every event on the
feed, with generated identifiers replaced by the labels the script gave them.
The photographs must match, and so must the outcome of every step — success,
or a refusal with the same code. Anything reachable one way and not another is
a difference this module names, by scenario, step and transport.

What it does not do: judge behaviour. Whether the core is *right* is the job of
the unit tests. This asks only whether each transport is the core.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

from agentco import auth, db, events, leases, policy, snapshots
from agentco.errors import Refusal
from agentco.outbox import PUSH_VERBS, Outbox, drain, registry_publisher
from agentco.publish import Registry, RegistryError
from agentco.refusals import classify
from agentco.sop import SopLibrary
from agentco.work import CapabilityError, Queue, parse_terminal_status, unknown_item

TRANSPORTS = ("http", "mcp", "mcp-remote", "outbox")

#: Everything the plane reads from the environment to find a store or a
#: registry. `run_scenario` and the CLI's budget probe pin all of them to
#: "unset" so a run can only ever touch the temporary stores it was built on —
#: the second party on P5.V ran `conform` with AGENTCO_DB set and watched it
#: write 29 items into the operator's live database, then found the budget
#: probe doing the same after the scenarios were fixed. The pin is per call
#: site, so a new site that opens a store must wrap itself the same way.
STORE_ENV_VARS = (
    "AGENTCO_DB", "AGENTCO_REGISTRY_DB", "AGENTCO_WORK_STORE", "AGENTCO_SOP_STORE",
    "AGENTCO_REGISTRY_URL", "AGENTCO_SECRET", "AGENTCO_REGISTRY_KEYS", "AGENTCO_ACTOR",
    "AGENTCO_CAPABILITIES", "AGENTCO_HUMANS", "AGENTCO_VERIFIERS", "AGENTCO_PROTECTED_TAGS",
    "AGENTCO_OUTBOX", "AGENTCO_AGENT_LABEL", "AGENTCO_REGISTRY_OPERATOR",
)

#: The verbs a scenario may use. Every transport carries a subset; the core
#: carries all of them, which is what makes the core the reference.
VERBS = (
    "claim_scope", "release_scope", "snapshot",
    "work_create", "work_pull", "work_report", "attest", "adjudicate",
    "sop_create", "sop_revise", "sop_activate", "sop_instantiate", "sop_propose",
)

#: What each transport carries, as the ladder documents it. HTTP carries every
#: verb; MCP the twelve-tool roster (adjudication rides on `attest`; creating
#: and instantiating procedures are HTTP-only by design); the outbox the push
#: set. A verb a transport does not carry is performed through the core in that
#: transport's run — the question is not "can the outbox create work" (it
#: cannot, by decision) but "does what it does carry mean the same thing".
CARRIES: dict[str, frozenset[str]] = {
    "core": frozenset(VERBS),
    "http": frozenset(VERBS),
    "mcp": frozenset({
        "claim_scope", "release_scope", "snapshot",
        "work_create", "work_pull", "work_report", "attest",
        "sop_revise", "sop_activate",
    }),
    # The same tools, proxied to the registry over HTTP: a fourth relay with
    # its own field mapping (`_RemoteBackend`), conformed like the other three.
    "mcp-remote": frozenset({
        "claim_scope", "release_scope", "snapshot",
        "work_create", "work_pull", "work_report", "attest",
        "sop_revise", "sop_activate",
    }),
    "outbox": frozenset(PUSH_VERBS),
}

KEYS = {
    "alice": "alice-secret",      # executes
    "bob": "bob-secret",          # verifies
    "carol": "carol-secret",      # reviews, adjudicates, may be declared human
    "operator": "operator-secret",
}


class ConformanceError(Exception):
    """A scenario step could not be performed at all — a harness bug, not a finding."""


# --------------------------------------------------------------------------- #
# scenarios
# --------------------------------------------------------------------------- #


def step(actor: str, verb: str, save: Optional[str] = None, **args: Any) -> dict:
    """One line of a script. `save` names the identifier the step produces."""
    if verb not in VERBS:
        raise ConformanceError(f"unknown verb {verb!r}")
    return {"actor": actor, "verb": verb, "args": args, "save": save}


JUDGED = {"kind": "judged", "check": "a reviewer reads the diff", "max_park_seconds": 900,
          "on_timeout": "escalate", "escalate_to": "carol"}
DETERMINISTIC = {"kind": "deterministic", "check": "pytest -q", "max_park_seconds": 900,
                 "on_timeout": "fail"}


def _attestation(check: str, exit_status: int = 0) -> dict:
    return {"check": check, "exit_status": exit_status, "environment": "conformance",
            "at": "2026-09-02T15:00:00+00:00"}


SCENARIOS: dict[str, dict] = {
    "scope": {
        "steps": [
            step("alice", "claim_scope", save="lease", repo="org/repo", prefixes=["src/api"], intent="implement"),
            step("bob", "claim_scope", save="lease2", repo="org/repo", prefixes=["src/api/routes"], intent="review"),
            step("alice", "release_scope", lease_uid="@lease"),
            step("bob", "release_scope", lease_uid="lease_nope"),
            step("alice", "snapshot", repo="org/repo", artifact_uri="docs://spec", purpose="reading the spec"),
        ],
    },
    "work": {
        "steps": [
            step("operator", "work_create", save="t1", title="export the ledger"),
            step("alice", "work_pull", save="pull1"),
            step("alice", "work_report", item="@t1", attempt="@pull1.attempt", status="in_progress"),
            step("alice", "work_report", item="@t1", attempt="@pull1.attempt", status="nonsense"),
            step("alice", "work_report", item="w-deadbeef", attempt=1, status="done"),
            step("alice", "work_report", item="@t1", attempt="@pull1.attempt", status="done", result="exported"),
            step("bob", "work_report", item="@t1", attempt="@pull1.attempt", status="done", result="again"),
            step("alice", "work_pull", save="pull2"),
            step("operator", "work_create", save="t2", title="render the report", requires=["gpu"]),
            step("alice", "work_pull", save="pull3", capabilities=[]),
            step("bob", "work_pull", save="pull4", capabilities=["gpu"], ttl_seconds=4321),
        ],
    },
    "judged-gate": {
        "steps": [
            step("operator", "work_create", save="g1", title="migrate the schema", verify=JUDGED),
            step("alice", "work_pull", save="pull1", capabilities=[]),
            step("alice", "work_report", item="@g1", attempt="@pull1.attempt", status="done"),
            step("alice", "attest", item="@g1", attestation=_attestation(JUDGED["check"]), capabilities=["verify"]),
            step("bob", "attest", item="w-deadbeef", attestation=_attestation(JUDGED["check"]), capabilities=["verify"]),
            step("bob", "attest", item="@g1", attestation=_attestation(JUDGED["check"]), capabilities=["verify"],
                 adjudication={"verdict": "good", "evidence": "step 2 was redundant"}),
            step("carol", "adjudicate", item="@g1", verdict="bad", evidence="a second opinion"),
            step("carol", "adjudicate", item="w-deadbeef", verdict="bad", evidence="nothing"),
        ],
    },
    "deterministic-gate": {
        "steps": [
            step("operator", "work_create", save="d1", title="run the export", verify=DETERMINISTIC),
            step("alice", "work_pull", save="pull1"),
            step("alice", "work_report", item="@d1", attempt="@pull1.attempt", status="done",
                 attestation=_attestation("pytest -q", 1)),
            step("alice", "attest", item="@d1", attestation=_attestation("pytest -q", 0)),
            step("bob", "attest", item="@d1", attestation=_attestation("pytest -q", 0),
                 adjudication={"verdict": "good", "evidence": "x"}),
        ],
    },
    "adjudication": {
        "steps": [
            step("operator", "work_create", save="a1", title="fix the invoice"),
            step("alice", "work_pull", save="pull1"),
            step("alice", "work_report", item="@a1", attempt="@pull1.attempt", status="done"),
            step("alice", "adjudicate", item="@a1", verdict="good", evidence="my own shortcut"),
            step("carol", "adjudicate", item="@a1", verdict="bad", evidence="skipped the reproduce step"),
            step("bob", "adjudicate", item="@a1", verdict="good", evidence="a second opinion"),
        ],
    },
    "procedure": {
        "humans": ["carol"],
        "steps": [
            step("carol", "sop_create", save="deploy", title="deploy", purpose="ship it",
                 definition_of_done="the service answers"),
            step("carol", "sop_activate", sop="@deploy", version=1),
            step("alice", "sop_revise", sop="@deploy", changes={"common_mistakes": ["ran the migration last"]}),
            step("alice", "sop_activate", sop="@deploy", version=2),
            step("carol", "sop_create", save="pay", title="pay the vendor", purpose="pay", tags=["money"],
                 executor="human"),
            step("carol", "sop_activate", sop="@pay", version=1),
            step("alice", "sop_revise", sop="@pay", changes={"purpose": "skip approval"}),
            step("carol", "sop_revise", sop="@pay", changes={"purpose": "pay, with approval"}),
            step("alice", "sop_activate", sop="@pay", version=2),
            step("carol", "sop_activate", sop="@pay", version=0),
            step("carol", "sop_activate", sop="sop-deadbeef", version=1),
            step("operator", "sop_instantiate", save="i1", sop="@deploy", metadata={"epic": "release-week"}),
            step("operator", "sop_instantiate", save="i2", sop="@pay"),
            step("operator", "sop_instantiate", save="i3", sop="@pay", verify={
                "kind": "human", "check": "the owner signs off", "verifier": "carol",
                "max_park_seconds": 86400, "on_timeout": "escalate", "escalate_to": "carol"}),
        ],
    },
    "lessons": {
        "humans": ["carol"],
        "steps": [
            step("carol", "sop_create", save="export", title="export", purpose="export the ledger",
                 definition_of_done="matches the fixture", common_mistakes=["typed by a person"]),
            step("carol", "sop_activate", sop="@export", version=1),
            step("operator", "sop_instantiate", save="i1", sop="@export"),
            step("alice", "work_pull", save="pull1"),
            step("alice", "work_report", item="@i1", attempt="@pull1.attempt", status="done", result="skipped the diff"),
            step("carol", "adjudicate", item="@i1", verdict="bad", evidence="skipped the diff and reported done"),
            step("operator", "sop_instantiate", save="i2", sop="@export"),
            step("alice", "work_pull", save="pull2"),
            step("alice", "work_report", item="@i2", attempt="@pull2.attempt", status="done"),
            step("carol", "adjudicate", item="@i2", verdict="good", evidence="the diff is redundant"),
            step("alice", "sop_propose", sop="@export"),
            step("alice", "sop_propose", sop="@export"),
            step("alice", "sop_propose", sop="sop-deadbeef"),
        ],
    },
    "decomposition": {
        "steps": [
            step("operator", "work_create", save="goal", title="the goal"),
            *[step("operator", "work_create", save=f"c{i}", title=f"unit {i}", metadata={"parent": "@goal"})
              for i in range(7)],
            step("operator", "work_create", save="c8", title="one too many", metadata={"parent": "@goal"}),
            step("operator", "work_create", save="fix", title="fix unit 0",
                 metadata={"parent": "@goal", "repairs": "@c0"}),
            step("operator", "work_create", save="orphan", title="loose end", metadata={"parent": "w-deadbeef"}),
        ],
    },
    "verifier-binding": {
        "verifiers": ["bob"],
        "steps": [
            step("operator", "work_create", save="g1", title="review me", verify=JUDGED),
            step("alice", "work_pull", save="pull1"),
            step("alice", "work_report", item="@g1", attempt="@pull1.attempt", status="done"),
            step("carol", "attest", item="@g1", attestation=_attestation(JUDGED["check"]), capabilities=["verify"]),
            step("bob", "attest", item="@g1", attestation=_attestation(JUDGED["check"]), capabilities=["verify"]),
        ],
    },
}


# --------------------------------------------------------------------------- #
# the world — one set of stores, reachable four ways
# --------------------------------------------------------------------------- #


class _LoopbackRegistry(Registry):
    """A real `Registry` whose wire is the real app in-process, signed for real."""

    def __init__(self, actor: str, client, via: Optional[str] = None):
        super().__init__(actor, KEYS[actor], "http://conformance.test", via=via)
        self.client = client

    def _call(self, method: str, path: str, body: Optional[dict] = None, query: str = "") -> dict:
        raw = json.dumps(body).encode() if body is not None else b""
        ts = str(int(time.time()))
        headers = {
            "X-AgentCo-Actor": self.actor,
            "X-AgentCo-Timestamp": ts,
            "X-AgentCo-Signature": auth.sign(self.secret, method, path, ts, raw),
            "Content-Type": "application/json",
        }
        if self.via:
            headers["X-AgentCo-Via"] = self.via
        response = self.client.request(method, f"{path}{query}", content=raw or None, headers=headers)
        if response.status_code >= 400:
            raise RegistryError(response.status_code, response.json())
        return response.json()


@contextmanager
def _environment(**values: Optional[str]) -> Iterator[None]:
    """Set the operator's declarations for the MCP surface, which reads them from
    the environment because that is where an operator puts them."""
    saved = {k: os.environ.get(k) for k in values}
    try:
        for k, v in values.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class World:
    """Fresh stores in a temp dir, plus every transport pointed at them."""

    def __init__(self, root: Path, humans: list[str], verifiers: list[str]):
        self.root = root
        self.humans = frozenset(humans)
        self.verifiers = frozenset(verifiers)
        self.db_path = str(root / "registry.sqlite3")
        self.work_path = str(root / "work.jsonl")
        self.sop_path = str(root / "sops.jsonl")
        self.conn = db.connect(self.db_path)
        self.queue = Queue(self.work_path, verifiers=verifiers)
        self.library = SopLibrary(self.sop_path)
        self.labels: dict[str, str] = {}     # label -> identifier
        self.saved: dict[str, Any] = {}      # label -> whatever a step produced
        self._client = None
        self._servers: dict[str, Any] = {}

    # -- transports, built on first use -------------------------------------

    @property
    def client(self):
        if self._client is None:
            from fastapi.testclient import TestClient

            from agentco.app import create_app

            self._client = TestClient(create_app(
                db_path=self.db_path, keys=KEYS, operator="operator",
                work_store=self.work_path, sop_store=self.sop_path,
                humans=sorted(self.humans), verifiers=sorted(self.verifiers),
            ))
        return self._client

    def server(self, actor: str, remote: bool = False):
        key = (actor, remote)
        if key not in self._servers:
            from agentco.mcp_server import create_server

            if remote:
                self._servers[key] = create_server(registry=_LoopbackRegistry(actor, self.client))
            else:
                self._servers[key] = create_server(
                    db_path=self.db_path, work_store=self.work_path, sop_store=self.sop_path, actor=actor,
                )
        return self._servers[key]

    def tool(self, actor: str, name: str, remote: bool = False) -> Callable:
        return self.server(actor, remote)._tool_manager.get_tool(name).fn

    def outbox(self, actor: str) -> Outbox:
        return Outbox(self.root / "outbox" / actor / ".agentco")

    def env(self) -> dict[str, Optional[str]]:
        """The whole store-finding environment, pinned: unset everything, then
        the operator declarations this scenario makes."""
        pinned: dict[str, Optional[str]] = {name: None for name in STORE_ENV_VARS}
        pinned["AGENTCO_HUMANS"] = ",".join(sorted(self.humans)) or None
        pinned["AGENTCO_VERIFIERS"] = ",".join(sorted(self.verifiers)) or None
        return pinned

    # -- labels ---------------------------------------------------------------

    def resolve(self, value: Any) -> Any:
        """`@label` → the identifier it was saved as; `@label.field` → a field of it."""
        if isinstance(value, str) and value.startswith("@"):
            name, _, field = value[1:].partition(".")
            if field:
                holder = self.saved.get(name)
                if not isinstance(holder, dict) or field not in holder:
                    raise ConformanceError(f"{value}: nothing saved under {name!r} with {field!r}")
                return holder[field]
            if name not in self.labels:
                raise ConformanceError(f"{value}: nothing saved under {name!r}")
            return self.labels[name]
        if isinstance(value, dict):
            return {k: self.resolve(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.resolve(v) for v in value]
        return value

    def relabel(self, text: Any) -> Any:
        """Replace every identifier the script named inside free text — lesson
        and proposal lines carry the item id they came from."""
        if not isinstance(text, str):
            return text
        for label, ident in self.labels.items():
            if isinstance(ident, str) and ident:
                text = text.replace(ident, f"@{label}")
        return text

    def label_of(self, identifier: Any) -> Any:
        for label, ident in self.labels.items():
            if ident == identifier:
                return f"@{label}"
        return "?" if isinstance(identifier, str) and identifier[:2] in ("w-", "so", "le", "sn") else identifier


# --------------------------------------------------------------------------- #
# outcomes
# --------------------------------------------------------------------------- #


def _ok(**extra: Any) -> dict:
    return {"state": "ok", **extra}


def _refused(code: str) -> dict:
    return {"state": "refused", "code": code}


def _code_of(message: str) -> str:
    """The code a rendered refusal starts with — `Refusal.__str__` puts it first,
    followed by a colon and a space. A code may itself contain a colon
    (`revision_policy:protected`), so the split is on the separator, not the
    character."""
    return message.split(": ", 1)[0].strip()


# --------------------------------------------------------------------------- #
# drivers — one function per transport, every verb the transport carries
# --------------------------------------------------------------------------- #


def _core(world: World, s: dict) -> dict:
    actor, verb, a = s["actor"], s["verb"], world.resolve(s["args"])
    kind = policy.kind_of(actor, world.humans)
    try:
        if verb == "claim_scope":
            out = leases.claim(world.conn, actor=actor, repo=a["repo"], prefixes=a["prefixes"], intent=a["intent"])
            return _ok(**_save(world, s, out["lease"]["uid"] if "lease" in out else out.get("leaseUid"), out))
        if verb == "release_scope":
            leases.release(world.conn, actor=actor, lease_uid=a["lease_uid"])
            return _ok()
        if verb == "snapshot":
            snapshots.take(world.conn, actor=actor, artifact_uri=a["artifact_uri"], purpose=a["purpose"])
            return _ok()
        if verb == "work_create":
            item = world.queue.create(
                a["title"], requires=a.get("requires", ()), verify=a.get("verify"), metadata=a.get("metadata"),
            )
            return _ok(**_save(world, s, item.id, {"id": item.id}))
        if verb == "work_pull":
            # Exactly the HTTP handler's loop: a misroute is skipped, anything
            # else the queue refuses is refused. A core that swallowed more than
            # the handler would hide a transport that swallowed less.
            for candidate in world.queue.ready(agent=actor):
                try:
                    leased = world.queue.claim(candidate.id, actor, capabilities=a.get("capabilities"),
                                               **({"ttl_seconds": a["ttl_seconds"]} if a.get("ttl_seconds") else {}))
                except CapabilityError:
                    continue
                if leased is not None:
                    return _ok(state="leased", **_save(world, s, leased.id,
                                                         {"id": leased.id, "attempt": leased.lease_attempt}))
            return {"state": "empty"}
        if verb == "work_report":
            out = world.queue.report_result(
                a["item"], int(a["attempt"]), parse_terminal_status(a["status"]), result=a.get("result"),
                attestation=a.get("attestation"), submitted_by=actor,
            )
            if out is None:
                raise unknown_item(a["item"], "fence this report against")
            return _ok(landed=out.status.value)
        if verb == "attest":
            out = world.queue.attest(a["item"], a["attestation"], submitted_by=actor,
                                     capabilities=a.get("capabilities"), adjudication=a.get("adjudication"))
            if out is None:
                raise unknown_item(a["item"], "attest against")
            return _ok(landed=out.status.value)
        if verb == "adjudicate":
            out = world.queue.adjudicate(a["item"], a["verdict"], a["evidence"], adjudicator=actor)
            if out is None:
                raise unknown_item(a["item"], "adjudicate")
            return _ok()
        if verb == "sop_propose":
            draft = world.library.propose(a["sop"], world.queue, author=actor, author_kind=kind)
            return _ok(drafted=draft.version if draft is not None else None)
        if verb == "sop_create":
            body = {k: v for k, v in a.items() if k != "title"}
            sop = world.library.create(a["title"], author=actor, author_kind=kind, **body)
            return _ok(**_save(world, s, sop.sop_id, {"id": sop.sop_id}))
        if verb == "sop_revise":
            sop = world.library.revise(a["sop"], title=a.get("title"), author=actor, author_kind=kind,
                                       **a.get("changes", {}))
            return _ok(version=sop.version)
        if verb == "sop_activate":
            world.library.activate(a["sop"], int(a["version"]), author=actor, author_kind=kind)
            return _ok()
        if verb == "sop_instantiate":
            item = world.library.instantiate(a["sop"], world.queue, verify=a.get("verify"),
                                             metadata=a.get("metadata"))
            return _ok(**_save(world, s, item.id, {"id": item.id}))
    except Exception as exc:  # noqa: BLE001 - classified below, never swallowed
        return _refused(classify(exc).code)
    raise ConformanceError(f"core does not perform {verb!r}")


def _http(world: World, s: dict) -> dict:
    actor, verb, a = s["actor"], s["verb"], world.resolve(s["args"])
    reg = _LoopbackRegistry(actor, world.client)
    try:
        if verb == "claim_scope":
            out = reg.claim_scope(a["repo"], a["prefixes"], a["intent"])
            return _ok(**_save(world, s, out["lease"]["uid"] if "lease" in out else out.get("leaseUid"), out))
        if verb == "release_scope":
            reg.release_scope(a["lease_uid"])
            return _ok()
        if verb == "snapshot":
            reg.snapshot(a["artifact_uri"], a["purpose"])
            return _ok()
        if verb == "work_create":
            fields = {k: v for k, v in a.items() if k != "title"}
            out = reg.work_create(a["title"], **fields)
            return _ok(**_save(world, s, out["item"]["id"], {"id": out["item"]["id"]}))
        if verb == "work_pull":
            out = reg.work_pull(capabilities=a.get("capabilities"), ttl_seconds=a.get("ttl_seconds"))
            if out["state"] != "leased":
                return {"state": "empty"}
            return _ok(state="leased", **_save(world, s, out["item"]["id"],
                                                 {"id": out["item"]["id"], "attempt": out["attempt"]}))
        if verb == "work_report":
            out = reg.work_report(a["item"], int(a["attempt"]), a["status"], result=a.get("result"),
                                  attestation=a.get("attestation"))
            return _ok(landed=out["item"]["status"])
        if verb == "attest":
            out = reg.attest(a["item"], a["attestation"], capabilities=a.get("capabilities"),
                             adjudication=a.get("adjudication"))
            return _ok(landed=out["item"]["status"])
        if verb == "adjudicate":
            reg.adjudicate(a["item"], a["verdict"], a["evidence"])
            return _ok()
        if verb == "sop_create":
            body = {k: v for k, v in a.items() if k != "title"}
            out = reg.sop_create(a["title"], **body)
            return _ok(**_save(world, s, out["sop"]["sop_id"], {"id": out["sop"]["sop_id"]}))
        if verb == "sop_revise":
            body = dict(a.get("changes", {}))
            if a.get("title") is not None:
                body["title"] = a["title"]
            out = reg.sop_revise(a["sop"], **body)
            return _ok(version=out["sop"]["version"])
        if verb == "sop_activate":
            reg.sop_activate(a["sop"], int(a["version"]))
            return _ok()
        if verb == "sop_instantiate":
            fields = {k: v for k, v in a.items() if k != "sop"}
            out = reg.sop_instantiate(a["sop"], **fields)
            return _ok(**_save(world, s, out["item"]["id"], {"id": out["item"]["id"]}))
        if verb == "sop_propose":
            out = reg._call("POST", f"/sops/{a['sop']}/propose", {})
            return _ok(drafted=(out.get("sop") or {}).get("version"))
    except RegistryError as exc:
        payload = exc.payload if isinstance(exc.payload, dict) else {}
        return _refused(payload.get("code") or f"http_{exc.status}")
    raise ConformanceError(f"http does not perform {verb!r}")


def _mcp(world: World, s: dict, remote: bool = False) -> dict:
    actor, verb, a = s["actor"], s["verb"], world.resolve(s["args"])
    caps = a.get("capabilities")
    env = {**world.env(), "AGENTCO_CAPABILITIES": ",".join(caps) if caps else None}
    tool = lambda name: world.tool(actor, name, remote)  # noqa: E731
    with _environment(**env):
        try:
            if verb == "claim_scope":
                out = tool("claim_scope")(repo=a["repo"], prefixes=a["prefixes"], intent=a["intent"])
                return _ok(**_save(world, s, out["lease"]["uid"] if "lease" in out else out.get("leaseUid"), out))
            if verb == "release_scope":
                tool("release_scope")(lease_uid=a["lease_uid"])
                return _ok()
            if verb == "snapshot":
                tool("snapshot")(artifact_uri=a["artifact_uri"], purpose=a["purpose"])
                return _ok()
            if verb == "work_create":
                out = tool("work_create")(**{k: v for k, v in a.items()})
                return _ok(**_save(world, s, out["id"], {"id": out["id"]}))
            if verb == "work_pull":
                out = tool("work_pull")(**({"ttl_seconds": a["ttl_seconds"]} if a.get("ttl_seconds") else {}))
                if out is None:
                    return {"state": "empty"}
                return _ok(state="leased", **_save(world, s, out["id"], {"id": out["id"], "attempt": out["lease_attempt"]}))
            if verb == "work_report":
                out = tool("work_report")(item_id=a["item"], attempt=int(a["attempt"]), status=a["status"],
                                          result=a.get("result"), attestation=a.get("attestation"))
                return _ok(landed=out["status"])
            if verb == "attest":
                out = tool("attest")(item_id=a["item"], attestation=a["attestation"],
                                     adjudication=a.get("adjudication"))
                return _ok(landed=out["status"])
            if verb == "sop_revise":
                out = tool("sop_revise")(sop_id=a["sop"], changes=a.get("changes", {}), title=a.get("title"))
                return _ok(version=out["version"])
            if verb == "sop_activate":
                tool("sop_activate")(sop_id=a["sop"], version=int(a["version"]))
                return _ok()
        except Exception as exc:  # noqa: BLE001 - ToolError carries the code first
            return _refused(_code_of(str(exc)))
    raise ConformanceError(f"mcp does not perform {verb!r}")


def _mcp_remote(world: World, s: dict) -> dict:
    return _mcp(world, s, remote=True)


def _outbox(world: World, s: dict) -> dict:
    actor, verb, a = s["actor"], s["verb"], world.resolve(s["args"])
    payload: dict
    if verb == "claim_scope":
        payload = {"repo": a["repo"], "prefixes": a["prefixes"], "intent": a["intent"]}
    elif verb == "release_scope":
        payload = {"leaseUid": a["lease_uid"]}
    elif verb == "snapshot":
        payload = {"artifactUri": a["artifact_uri"], "purpose": a["purpose"]}
    elif verb == "work_report":
        payload = {"itemId": a["item"], "attempt": int(a["attempt"]), "status": a["status"],
                   "result": a.get("result"), "attestation": a.get("attestation")}
    elif verb == "attest":
        payload = {"itemId": a["item"], "attestation": a["attestation"], "capabilities": a.get("capabilities"),
                   "adjudication": a.get("adjudication")}
    elif verb == "adjudicate":
        payload = {"itemId": a["item"], "verdict": a["verdict"], "evidence": a["evidence"]}
    elif verb == "sop_revise":
        payload = {"sopId": a["sop"], "changes": a.get("changes", {}), "title": a.get("title")}
    elif verb == "sop_activate":
        payload = {"sopId": a["sop"], "version": int(a["version"])}
    else:
        raise ConformanceError(f"outbox does not perform {verb!r}")
    box = world.outbox(actor)
    line = box.push(verb, {k: v for k, v in payload.items() if v is not None})
    drain(box, registry_publisher(_LoopbackRegistry(actor, world.client, via="outbox")))
    receipt = _last_receipt(box, line)
    if receipt.get("state") == "refused":
        return _refused(receipt.get("code") or "refused")
    if receipt.get("state") != "published":
        return {"state": receipt.get("state", "unknown"), "detail": receipt.get("detail")}
    result = receipt.get("result") or {}
    if verb == "claim_scope":
        uid = result.get("leaseUid") or (result.get("lease") or {}).get("uid")
        return _ok(**_save(world, s, uid, result))
    if verb in ("work_report", "attest"):
        return _ok(landed=result.get("status") or (result.get("item") or {}).get("status"))
    if verb == "sop_revise":
        return _ok(version=result.get("version"))
    return _ok()


def _last_receipt(box: Outbox, line: Any) -> dict:
    """The receipt the drain just wrote. One box per actor, one line per step,
    drained immediately — so the newest receipt is this step's."""
    receipts = box.read_receipts()
    if not receipts:
        raise ConformanceError("the drainer left no receipt for the line it was given")
    return receipts[-1]


def _save(world: World, s: dict, identifier: Any, produced: Any) -> dict:
    if s.get("save"):
        world.labels[s["save"]] = identifier
        world.saved[s["save"]] = produced if isinstance(produced, dict) else {"id": identifier}
    return {}


DRIVERS: dict[str, Callable[[World, dict], dict]] = {
    "core": _core, "http": _http, "mcp": _mcp, "mcp-remote": _mcp_remote, "outbox": _outbox,
}


# --------------------------------------------------------------------------- #
# the photograph
# --------------------------------------------------------------------------- #


def _ttl_of(item) -> Optional[int]:
    """The lease's length, not its timestamps: a transport that dropped or
    rewrote ttlSeconds shows up here without the clock getting a vote."""
    if not item.lease_expires_at or not item.updated_at:
        return None
    from datetime import datetime

    expires = datetime.fromisoformat(item.lease_expires_at)
    updated = datetime.fromisoformat(item.updated_at)
    return int(round((expires - updated).total_seconds()))


def photograph(world: World) -> dict:
    """Everything that matters about the world, with identifiers replaced by labels."""
    items = []
    for item in sorted(world.queue.list(), key=lambda i: i.created_at):
        meta = item.metadata or {}
        adjudication = meta.get("adjudication") or {}
        report = meta.get("lease_report") or {}
        attestation = item.attestation or {}
        review = meta.get("plan_vs_actual") or {}
        items.append({
            "item": world.label_of(item.id),
            "title": item.title,
            "status": item.status.value,
            "result": world.relabel(item.result),
            "requires": list(item.requires),
            "blocked_by": sorted(world.label_of(b) for b in item.blocked_by),
            "leased_by": item.leased_by,
            "lease_ttl_s": _ttl_of(item),
            "lease_attempt": item.lease_attempt,
            "claims": [c.get("agent") for c in (meta.get("claims") or [])],
            "executor": report.get("reported_by"),
            "report": {"attempt": report.get("attempt"), "status": report.get("status")} if report else None,
            "gate": item.verify,
            "attestation": {k: attestation.get(k) for k in ("submitted_by", "exit_status", "check", "environment", "at")}
            if attestation else None,
            "verify_failures": item.verify_failures,
            "verify_resolution": meta.get("verify_resolution"),
            "adjudication": {k: adjudication.get(k) for k in ("verdict", "by", "evidence", "executors", "proposed_in")}
            if adjudication else None,
            "sop": world.label_of((meta.get("sop_ref") or {}).get("sop_id")) if meta.get("sop_ref") else None,
            "sop_version": (meta.get("sop_ref") or {}).get("version"),
            "sop_plan": meta.get("sop_plan"),
            "review": {k: review.get(k) for k in ("flags", "plan", "verdict")} | {
                "actual": {k: v for k, v in (review.get("actual") or {}).items()
                           if k not in ("filed_at", "reported_at")}
            } if review else None,
            "parent": world.label_of(meta["parent"]) if meta.get("parent") else None,
            "repairs": world.label_of(meta["repairs"]) if meta.get("repairs") else None,
            "other_metadata": sorted(k for k in meta if k not in (
                "lease_report", "claims", "adjudication", "sop_ref", "sop_plan", "plan_vs_actual",
                "parent", "repairs", "verify_resolution", "verify_parked_at", "verify_retry",
            )),
        })
    sops = []
    for sop_id in sorted({s.sop_id for s in world.library._read_all()}, key=lambda i: world.label_of(i)):
        for version in world.library.history(sop_id):
            sops.append({
                "sop": world.label_of(sop_id),
                "version": version.version,
                "status": version.status.value,
                "title": version.title,
                **{field: getattr(version, field) for field in (
                    "purpose", "trigger", "entry_check", "inputs", "definition_of_done",
                    "validation", "write_back", "next_sop", "executor", "author", "author_kind",
                    "superseded_by",
                )},
                "common_mistakes": [world.relabel(m) for m in version.common_mistakes],
                "proposals": [world.relabel(p) for p in version.proposals],
                "tags": list(version.tags),
            })
    feed = []
    for event in events.read(world.conn, limit=1000)["events"]:
        payload = event.get("payload") or {}
        feed.append({
            "kind": event["kind"],
            "actor": event["actor"],
            "repo": event.get("repo"),
            "lease": world.label_of(payload.get("leaseUid")) if payload.get("leaseUid") else None,
            "payload": sorted(payload),
            **{k: payload.get(k) for k in (
                "prefixes", "intent", "holder", "holderAttested", "withHolder", "myIntent", "theirIntent",
                "overlaps", "artifactUri", "purpose", "action", "resolution", "hashKind",
            )},
        })
    return {"items": items, "sops": sops, "events": feed}


# --------------------------------------------------------------------------- #
# running and comparing
# --------------------------------------------------------------------------- #


def run_scenario(name: str, transport: str, root: Optional[Path] = None) -> dict:
    """One scenario, with every step the transport carries performed through it.

    Returns the outcomes step by step and the photograph at the end. `core` is
    the reference run; the others are compared against it.
    """
    scenario = SCENARIOS[name]
    carries = CARRIES[transport]
    logging.getLogger("httpx").setLevel(logging.WARNING)  # the wire is not the finding
    with tempfile.TemporaryDirectory(prefix=f"agentco-conform-{name}-{transport}-") as tmp, \
            _environment(**{k: None for k in STORE_ENV_VARS}):
        world = World(Path(root or tmp), scenario.get("humans", []), scenario.get("verifiers", []))
        outcomes = []
        with _environment(**world.env()):
            for s in scenario["steps"]:
                via = transport if s["verb"] in carries else "core"
                outcome = DRIVERS[via](world, s)
                outcomes.append({"step": f"{s['actor']} {s['verb']}", "via": via, **outcome})
            picture = photograph(world)
        world.conn.close()
    return {"scenario": name, "transport": transport, "outcomes": outcomes, "state": picture}


def _diff(path: str, expected: Any, actual: Any, out: list[str]) -> None:
    if isinstance(expected, dict) and isinstance(actual, dict):
        for key in sorted(set(expected) | set(actual)):
            _diff(f"{path}.{key}", expected.get(key, "<absent>"), actual.get(key, "<absent>"), out)
    elif isinstance(expected, list) and isinstance(actual, list):
        if len(expected) != len(actual):
            out.append(f"{path}: {len(expected)} entries in core, {len(actual)} here")
        for i, (e, a) in enumerate(zip(expected, actual)):
            _diff(f"{path}[{i}]", e, a, out)
    elif expected != actual:
        out.append(f"{path}: core={expected!r} here={actual!r}")


def compare(name: str, transports: tuple = TRANSPORTS) -> dict:
    """Run a scenario through the core and each transport; name every difference."""
    reference = run_scenario(name, "core")
    report = {"scenario": name, "steps": len(reference["outcomes"]), "transports": {}}
    for transport in transports:
        run = run_scenario(name, transport)
        diffs: list[str] = []
        for i, (e, a) in enumerate(zip(reference["outcomes"], run["outcomes"])):
            e_cmp = {k: v for k, v in e.items() if k != "via"}
            a_cmp = {k: v for k, v in a.items() if k != "via"}
            if e_cmp != a_cmp:
                diffs.append(f"step {i + 1} ({a['step']} via {a['via']}): core={e_cmp} here={a_cmp}")
        _diff("state", reference["state"], run["state"], diffs)
        carried = sum(1 for o in run["outcomes"] if o["via"] == transport)
        report["transports"][transport] = {"conforms": not diffs, "carried": carried, "diffs": diffs}
    return report


def conformance_report(names: Optional[list[str]] = None, transports: tuple = TRANSPORTS) -> dict:
    """Every scenario, every transport. `conforms` is true only when nothing differs anywhere."""
    reports = [compare(n, transports) for n in (names or list(SCENARIOS))]
    failures = [
        f"{r['scenario']} / {t}: {d}"
        for r in reports for t, res in r["transports"].items() for d in res["diffs"]
    ]
    return {"scenarios": reports, "failures": failures, "conforms": not failures}
