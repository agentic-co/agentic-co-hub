"""Per-actor route allowlist, and the one rule for who a read is "as".

Authentication says WHO is asking (`auth.py`). This module says WHICH VERBS
that actor may reach at all, and it exists for one kind of credential: the
transport. A relay that carries a person's signed answer to the hub must not be
able to read the queue, file work, or read anyone's results; if it could, a
stolen relay key would be a stolen read of the whole hub. ADR 0008 §3.3: "a
credential that reaches, by a per-actor route allowlist, only the answer route
and its own receipts."

**Configured in the key table's object form**, next to `owner` and `clearance`:

    {"relay-1": {"secret": "...", "routes": ["attest", "events"]}}

`routes` names the verbs `_handle` is called with (the second argument every
route passes, e.g. `"attest"`, `"events"`, `"work_pull"`; `VERBS` below is the
full list and a test keeps it equal to the verbs `app.py` uses).

  * **Absent `routes` = unrestricted.** Every table written before this existed
    keeps working; restriction is something an operator adds on purpose.
  * **An empty list reaches nothing.** It is a credential that authenticates and
    can do nothing, which is a legitimate way to park one.
  * **A malformed `routes` fails CLOSED for that actor** (reaches nothing), and
    only that actor: signing must not start refusing a whole table because one
    entry is mistyped, exactly as for a malformed `clearance`.
  * **An unknown name grants nothing.** A typo narrows access, never widens it.

**Read per request, from the key table.** An edit applies on the next request,
as revocation does; a cache keyed on the file's stat keeps that to one `stat`.

**The partition transport credential class** (ADR 0008 §8 B1/B2) is declared
with `"class"` in the same object form, and is a different thing from `routes`:

    {"relay-a": {"secret": "...", "owner": "dana", "class": "transport"}}

  * `owner` is REQUIRED: the person the relay acts for (its *party*, by
    `auth.owner_of`). A transport entry with no owner, or with a `class` this
    build does not know, is not loaded at all, so it authenticates nobody.
    It cannot be the owner's own key (an entry may not own itself).
  * It reaches `TRANSPORT_VERBS` and nothing else; handlers narrow each
    further (see `app.py`): `GET /hub`; `GET /work?ready=1` filtered to its
    party, no other query parameter; `GET /work/{id}` for its party's vehicles
    and what they verify; `GET /events` for `HumanKey*` kinds only. A `routes`
    list beside `class` can only NARROW that set, never widen it.
  * `TRANSPORT_ANSWER_VERBS`: `POST /answers`, `GET /answers/{key_id}/{nonce}`
    and `GET /answers/held` (agentco/envelope.py). The answer route takes only
    its party's signed answers; the receipt read only what this credential
    carried; the held list only its party's, without verdicts.

**Group `transport`.** `"routes": ["transport"]` expands to the answer route
and its receipts read: what a relay needs when it is not a transport-class
credential but should reach no more than one.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from agentco.errors import Refusal

#: Every verb `_handle` is called with. `tests/test_authz.py` parses `app.py`
#: and fails when this and the verbs it uses differ in either direction, so a
#: route added without a decision here cannot ship.
VERBS: frozenset[str] = frozenset({
    "adjudicate", "answer_held", "answer_receipt", "answer_submit", "approval_get", "approval_record", "attest", "chain_adopt", "chain_get",
    "chain_impact", "chain_manifest", "claim_scope", "digest_receive", "divergence", "events",
    "gate_reassign",
    "hub_get", "memory_clearance", "memory_create", "memory_get", "memory_history", "memory_list",
    "memory_pin", "memory_rate", "memory_reconsolidate", "memory_replay", "memory_retire",
    "memory_revise", "memory_revive", "memory_reviews", "memory_search", "memory_space", "memory_state",
    "memory_trace", "metrics", "promote", "release_scope", "run_get", "run_list", "run_pack",
    "run_transfer", "snapshot", "sop_activate", "sop_create", "sop_get", "sop_list",
    "sop_outcomes", "sop_propose", "sop_proposals", "sop_retire", "sop_revise", "sop_run",
    "view_me", "work_create", "work_get", "work_list", "work_pull", "work_renew", "work_report",
})

#: The answer route and the receipts read (ADR 0008 §8 B2).
TRANSPORT_ANSWER_VERBS: frozenset[str] = frozenset({"answer_submit", "answer_receipt", "answer_held"})

#: Named verb groups (see the module docstring).
PROFILES: dict[str, frozenset[str]] = {"transport": TRANSPORT_ANSWER_VERBS}

#: Every verb a `"class": "transport"` credential may reach at all.
TRANSPORT_VERBS: frozenset[str] = frozenset({"hub_get", "work_list", "work_get", "events"}) | TRANSPORT_ANSWER_VERBS

_CACHE: dict[str, tuple[tuple[int, int], dict[str, frozenset[str]]]] = {}


def parse_routes(value: object) -> frozenset[str]:
    """One `routes` value to the set of verbs it reaches; malformed reaches nothing."""
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        return frozenset()
    out: set[str] = set()
    for name in value:
        name = name.strip()
        if name in PROFILES:
            out |= PROFILES[name]
        elif name in VERBS:
            out.add(name)
    return frozenset(out)


def actors_from_key_table() -> dict[str, frozenset[str]]:
    """actor -> the verbs it may reach, for actors that carry `routes` only.

    Read separately from `auth.load_identities` so a malformed `routes` cannot
    make signing refuse the table; an unreadable table restricts nobody here
    because it authenticates nobody there (`load_keys` returns empty).
    """
    from agentco import auth

    target = os.environ.get(auth.KEYS_ENV_VAR)
    if not target:
        return {}
    path = Path(target)
    try:
        stamp = path.stat()
    except OSError:
        _CACHE.pop(str(path), None)
        return {}
    fingerprint = (stamp.st_mtime_ns, stamp.st_size)
    cached = _CACHE.get(str(path))
    if cached and cached[0] == fingerprint:
        return cached[1]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        _CACHE.pop(str(path), None)
        return {}
    out: dict[str, frozenset[str]] = {}
    for actor, value in (data.items() if isinstance(data, dict) else ()):
        if not isinstance(value, dict):
            continue
        verbs: Optional[frozenset[str]] = parse_routes(value["routes"]) if "routes" in value else None
        if "class" in value:
            valid = value["class"] == "transport" and isinstance(value.get("owner"), str) and value["owner"].strip()
            # a class that is not understood reaches nothing (and is not loaded
            # by `auth` either); `routes` beside it can only narrow
            verbs = (TRANSPORT_VERBS if verbs is None else verbs & TRANSPORT_VERBS) if valid else frozenset()
        if verbs is not None:
            out[str(actor)] = verbs
    _CACHE[str(path)] = (fingerprint, out)
    return out


class Routes:
    """Which verbs an actor may reach. Injected values win over the key table."""

    def __init__(self, static: Optional[Mapping[str, object]] = None,
                 *, source: Optional[Callable[[], Mapping[str, frozenset[str]]]] = None,
                 transports: Optional[Mapping[str, str]] = None):
        self._static = {a: parse_routes(list(v) if isinstance(v, (set, frozenset, tuple)) else v)
                        for a, v in (static or {}).items()}
        self._source = source
        #: injected transport-class actors: actor -> owner (the party)
        self._transports = {str(a): str(o) for a, o in (transports or {}).items() if o}
        for actor in self._transports:
            self._static[actor] = self._static.get(actor, TRANSPORT_VERBS) & TRANSPORT_VERBS

    def transport_party(self, actor: str) -> Optional[str]:
        """The person a transport-class credential acts for, or None for any other actor."""
        if actor in self._transports:
            return self._transports[actor]
        if self._source is None:
            return None
        from agentco import auth

        ident = auth.load_identities().get(actor)
        return ident.owner if ident and ident.klass == "transport" else None

    def allowed(self, actor: str) -> Optional[frozenset[str]]:
        """The actor's verbs, or None when it is unrestricted."""
        if actor in self._static:
            return self._static[actor]
        return self._source().get(actor) if self._source is not None else None

    def check(self, actor: str, verb: str) -> None:
        allowed = self.allowed(actor)
        if allowed is not None and verb not in allowed:
            raise route_not_allowed(actor, verb)


def build(static: Optional[Mapping[str, object]] = None,
          transports: Optional[Mapping[str, str]] = None) -> Routes:
    return Routes(static, source=None if static is not None else actors_from_key_table,
                  transports=transports)


def route_not_allowed(actor: str, verb: str) -> Refusal:
    return Refusal(
        code="route_not_allowed",
        message=f"{actor!r} is not allowed to reach {verb!r} on this hub",
        remediation=(
            "This credential is restricted to the routes in its key-table entry. "
            f"The operator widens it by adding {verb!r} to that entry's \"routes\" "
            "list (it applies on the next request), or removes \"routes\" to lift "
            "the restriction; use a credential that carries this route instead."
        ),
        http_status=403,
    )


def read_agent(actor: str, asked: Optional[str], operator: str) -> Optional[str]:
    """The agent a ready-work read is filtered by: the SIGNER, never the query.

    ADR 0008 §6. A read credential that could name its own agent could read
    what is ready for anyone, which is the work, assignees and gate metadata of
    everyone else. A query `agent` naming another actor is refused
    `agent_from_signature`; naming oneself is redundant and accepted.

    **The one exception is the declared operator**, who may name any agent or
    none (the whole ready set). Judged safe because the operator already reads
    the entire queue, results and gates included, through the unfiltered
    `GET /work`, so the filter protects nothing from it; and it is the only
    way an operator sees queue health across lanes. It is the registry's
    operator (`AGENTCO_REGISTRY_OPERATOR`), not any credential that happens to
    be an administrator or a human.
    """
    if actor == operator:
        return asked or None
    if asked and asked != actor:
        raise agent_from_signature(actor, asked)
    return actor


def check_read_agent(actor: str, asked: Optional[str], operator: str) -> None:
    """The same refusal for a read that has no agent filter (a plain list)."""
    if asked and actor != operator and asked != actor:
        raise agent_from_signature(actor, asked)


def agent_from_signature(actor: str, asked: str) -> Refusal:
    return Refusal(
        code="agent_from_signature",
        message=f"{actor!r} asked for work as {asked!r}; the agent of a read comes from the signature",
        remediation=(
            "Drop the 'agent' parameter: the hub filters by the actor that signed "
            "the request. To read as another actor, sign as that actor."
        ),
        http_status=400,
    )


def transport_query_refused(actor: str, names: list[str]) -> Refusal:
    return Refusal(
        code="transport_query_refused",
        message=f"{actor!r} is a transport credential; its ready read takes no parameter but 'ready' (got {sorted(names)})",
        remediation=(
            "Send GET /work?ready=1 and nothing else. The hub filters to the party the "
            "credential acts for; a filter that could widen that is refused, not ignored."
        ),
        http_status=400,
    )


def party_owns(party: str, assigned: Optional[str], identities: Mapping[str, Any]) -> bool:
    """Whether an item assigned to `assigned` belongs to `party`: the party
    itself, or an actor the key table says it owns. Unassigned belongs to nobody."""
    from agentco import auth

    return bool(assigned) and auth.owner_of(assigned, identities) == party
