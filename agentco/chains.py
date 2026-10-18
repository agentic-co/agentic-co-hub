"""Chain manifests, adoptions and approvals: the v3.6 records a person stands behind.

ASOP v3.6 (asop-spec 0.6.0) adds three records an engine cannot keep honestly
on its own, because each one names a person or names the store:

  * **The chain manifest** (`asop.chain-manifest/1`, §3.10) pins every
    procedure a chain can reach to one version, by the STORE's digest of that
    version ("a member's digest is the store's"). This plane is the store, so
    it is the one place that can say whether a manifest's digests are true.
  * **The adoption** (`asop.adoption/1`, §3.10) moves a running chain to
    another manifest. A declared human records it, and its impact report is
    COMPUTED from the two manifests, never typed. This plane holds the declared
    humans (`AGENTCO_HUMANS`), so it is where "only a person records one" can
    be enforced rather than asserted.
  * **The approval** (`asop.approval/1`, §3.7) binds a person's approval to the
    digest of exactly what they looked at, and optionally to the subset of
    subjects they approved. `approved_by` must be a declared human, "a
    cross-record rule, enforced where the approval is recorded".

The rules themselves are the contract's (`asop.manifest`); this module stores
what passed them and refuses what did not. **Feature-detected**: on an
installed contract older than 0.6.0 there is no `asop.manifest`, and every
entry point refuses `contract_unsupported` rather than storing a record it
cannot validate.

WHAT IS DELIBERATELY NOT HERE: the chain outcome record (§5.8) and the step
journal (§5.6). The outcome is "computed from the ledger, never typed", and this
plane holds no ledger of a runtime's runs — accepting a typed one would store
the very thing the spec forbids. They arrive with journal ingest.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Iterable, Mapping, Optional

from agentco import events, policy
from agentco.errors import Refusal, human_required, manifest_unknown
from agentco.sop import ASOP, CHAIN_KEY, SopContractError, SopStatus, asop_payload, validate_asop

try:  # asop-spec 0.6.0 (spec v3.6)
    from asop.chain import ChainContractError
    from asop.manifest import (
        ADOPTION_REQUIRED,
        ADOPTION_SCHEMA,
        adoption_impact,
        digest_of,
        is_pinned,
        manifest_digest,
        validate_adoption,
        validate_manifest,
    )

    SUPPORTED = True
except ImportError:  # 0.4.x / 0.5.x
    SUPPORTED = False
    ADOPTION_REQUIRED = "adoption_required"

APPROVAL_SCHEMA = "asop.approval/1"


def require_person(actor: str, humans: Iterable[str], what: str) -> None:
    """`human_required` for a non-human caller — checked first on the human-only
    chain verbs, before anything about the record (spec 0.6.1 §10)."""
    if policy.kind_of(actor, frozenset(humans)) != policy.HUMAN:
        raise human_required(f"{actor!r} is not a declared human; only a person may {what}",
                             "Ask a person the operator declared in AGENTCO_HUMANS.")


def _require_contract() -> None:
    if not SUPPORTED:
        raise Refusal(
            code="contract_unsupported",
            message="chain manifests, adoptions and approvals need asop-spec 0.6.0 (spec v3.6)",
            remediation=(
                "Upgrade the plane's asop-spec to 0.6.0 or later. The installed contract "
                "cannot validate these records, and storing one unvalidated would be "
                "worse than refusing it."
            ),
            http_status=501,
        )


def _invalid(code: str, exc: Exception) -> Refusal:
    return Refusal(
        code=code,
        message=str(exc),
        remediation="Correct the record and send it again; nothing was stored.",
        http_status=400,
    )


def _now() -> str:
    return events.now_iso()


# --------------------------------------------------------------------------- #
# The store's digest of one ASOP version
# --------------------------------------------------------------------------- #


def store_digest(asop: ASOP) -> Optional[str]:
    """`sha256:` over the canonical JSON of this version's VALIDATED body.

    The body is what `validate_asop` returns for the version's own payload, so
    two stores holding the same record compute the same string, and the
    identity fields (status, author, timestamps) that change without the
    procedure changing are not in it. `None` on a contract with no
    `asop.manifest`, which has no digest to compare against — and `None` for a
    stored version the installed contract no longer accepts (a legacy record
    from before a rule tightened), which therefore cannot be pinned: reading it
    must not fail because it cannot be digested.
    """
    if not SUPPORTED:
        return None
    try:
        body = validate_asop(asop_payload(asop), asop_id=asop.asop_id)
    except SopContractError:
        return None
    return digest_of(body)


# --------------------------------------------------------------------------- #
# Manifests
# --------------------------------------------------------------------------- #


def record_manifest(conn, library, *, actor: str, doc: Any,
                    agent_label: Optional[str] = None) -> dict:
    """Store a validated manifest whose every member this store holds, at the
    digest this store computes. Idempotent on the manifest's digest.

    The first manifest recorded for a chain is the one it STARTS under. Later
    ones are stored but take effect only through an adoption — recording a
    manifest is something an engine does, moving a chain is something a person
    does, and conflating the two would let the engine move it.
    """
    _require_contract()
    try:
        manifest = validate_manifest(doc)
    except ChainContractError as exc:
        raise _invalid("manifest_invalid", exc) from exc
    for member in manifest["members"]:
        asop = library.get(member["asop_id"], version=member["version"])
        if asop is None:
            raise Refusal(
                code="manifest_member_unknown",
                message=f"{member['asop_id']} v{member['version']} is not in this store",
                remediation="Pin only versions this store holds; a manifest pinning nothing resolves nothing.",
                http_status=409,
            )
        if asop.status == SopStatus.DRAFT:
            raise Refusal(
                code="manifest_member_unknown",
                message=f"{member['asop_id']} v{member['version']} is a DRAFT, which cannot be run",
                remediation="Activate it first, or pin the active version.",
                http_status=409,
            )
        ours = store_digest(asop)
        if ours is None or member["digest"] != ours:
            raise Refusal(
                code="manifest_digest_mismatch",
                message=(
                    f"{member['asop_id']} v{member['version']}: the manifest says "
                    f"{member['digest']}, this store's digest is {ours}"
                ),
                remediation=(
                    "A member's digest is the store's (ASOP.md §3.10). Take it from "
                    "GET /sops/{id}?version= (`digest`) rather than computing your own."
                ),
                http_status=409,
            )

    digest = manifest_digest(manifest)
    at = _now()
    with conn:
        inserted = conn.execute(
            "INSERT INTO chain_manifests(digest, chain, doc, recorded_by, recorded_at) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT (digest) DO NOTHING",
            (digest, manifest["chain"], json.dumps(manifest, sort_keys=True), actor, at),
        ).rowcount
        started = conn.execute(
            "INSERT INTO chains(chain, start_digest, current_digest, adoptions, started_at) "
            "VALUES (?, ?, ?, 0, ?) ON CONFLICT (chain) DO NOTHING",
            (manifest["chain"], digest, digest, at),
        ).rowcount
    if inserted:
        events.append(
            conn, kind="ChainManifestRecorded", actor=actor, agent_label=agent_label,
            occurred_at=at,
            payload={"chain": manifest["chain"], "digest": digest, "starts": bool(started)},
        )
    return {
        "state": "accepted" if inserted else "unchanged",
        "digest": digest,
        "chain": manifest["chain"],
        "effective": _current(conn, manifest["chain"]) == digest,
        "manifest": manifest,
    }


def _manifest(conn, digest: str) -> Optional[dict]:
    row = conn.execute("SELECT doc FROM chain_manifests WHERE digest = ?", (digest,)).fetchone()
    return json.loads(row["doc"]) if row else None


def _current(conn, chain: str) -> Optional[str]:
    row = conn.execute("SELECT current_digest FROM chains WHERE chain = ?", (chain,)).fetchone()
    return row["current_digest"] if row else None


def _unknown_chain(chain: str) -> Refusal:
    return Refusal(
        code="chain_unknown",
        message=f"no manifest has been recorded for chain {chain!r}",
        remediation="Record the chain's manifest first (POST /chains/manifests).",
        http_status=404,
    )


def chain_view(conn, chain: str) -> Optional[dict]:
    """The chain's effective manifest, every manifest recorded for it, and its
    adoptions in order. `None` for a chain nobody has pinned."""
    row = conn.execute(
        "SELECT start_digest, current_digest, adoptions, started_at FROM chains WHERE chain = ?",
        (chain,),
    ).fetchone()
    if row is None:
        return None
    manifests = conn.execute(
        "SELECT digest, recorded_by, recorded_at FROM chain_manifests WHERE chain = ? "
        "ORDER BY recorded_at, digest",
        (chain,),
    ).fetchall()
    adoptions = conn.execute(
        "SELECT doc FROM chain_adoptions WHERE chain = ? ORDER BY ordinal", (chain,)
    ).fetchall()
    return {
        "chain": chain,
        "startDigest": row["start_digest"],
        "currentDigest": row["current_digest"],
        "startedAt": row["started_at"],
        "manifest": _manifest(conn, row["current_digest"]),
        "manifests": [
            {"digest": m["digest"], "recordedBy": m["recorded_by"], "recordedAt": m["recorded_at"]}
            for m in manifests
        ],
        "adoptions": [json.loads(a["doc"]) for a in adoptions],
    }


# --------------------------------------------------------------------------- #
# Adoption
# --------------------------------------------------------------------------- #


def impact(conn, chain: str, to: str) -> dict:
    """What adopting manifest `to` would change for `chain` — computed, for a
    person to read BEFORE recording the adoption."""
    _require_contract()
    current = _current(conn, chain)
    if current is None:
        raise _unknown_chain(chain)
    old, new = _manifest(conn, current), _manifest(conn, to)
    if new is None or new["chain"] != chain:
        raise manifest_unknown(
            f"no manifest {to} is recorded for chain {chain!r}",
            "Record the target manifest for this chain first (POST /chains/manifests).",
        )
    return {"chain": chain, "from": current, "to": to, "impact": adoption_impact(old, new)}


def adopt(conn, *, chain: str, to: str, actor: str, humans: Iterable[str],
          reason: Optional[str] = None, agent_label: Optional[str] = None) -> dict:
    """Record a person's adoption of manifest `to` for `chain`.

    **Human-only, and the human is the signature.** `adopted_by` is the actor
    that signed, and the actor must be one the operator declared human. An
    agent may PROPOSE an adoption (§3.10) — by reading `impact` and asking —
    and may not record one.

    **The impact is the plane's, not the caller's.** It is computed here and
    the record validated by `validate_adoption`, which refuses any impact that
    is not the computed one; the body is never asked for it.

    **Compare-and-set on the chain's current manifest.** Two people adopting at
    once from the same manifest would otherwise fork the chain; the second
    update matches no row and is refused `work_conflict`.
    """
    _require_contract()
    if policy.kind_of(actor, frozenset(humans)) != policy.HUMAN:
        raise human_required(
            f"{actor!r} is not a declared human; only a person records an adoption",
            "Ask a person the operator declared in AGENTCO_HUMANS to record it. An "
            "agent may read GET /chains/{chain}/impact and propose; it may not adopt.",
        )
    preview = impact(conn, chain, to)
    old, new = _manifest(conn, preview["from"]), _manifest(conn, to)
    record = {
        "schema": ADOPTION_SCHEMA,
        "chain": chain,
        "from": preview["from"],
        "to": to,
        "adopted_by": actor,
        "adopted_at": _now(),
        "impact": preview["impact"],
    }
    if reason is not None:
        record["reason"] = reason
    try:
        record = validate_adoption(record, old=old, new=new)
    except ChainContractError as exc:
        raise _invalid("adoption_invalid", exc) from exc

    with conn:
        moved = conn.execute(
            "UPDATE chains SET current_digest = ?, adoptions = adoptions + 1 "
            "WHERE chain = ? AND current_digest = ?",
            (to, chain, record["from"]),
        ).rowcount
        if moved:
            ordinal = conn.execute(
                "SELECT adoptions FROM chains WHERE chain = ?", (chain,)
            ).fetchone()["adoptions"]
            conn.execute(
                "INSERT INTO chain_adoptions(chain, ordinal, from_digest, to_digest, doc, "
                "adopted_by, adopted_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (chain, ordinal, record["from"], to, json.dumps(record, sort_keys=True),
                 actor, record["adopted_at"]),
            )
    if not moved:
        raise Refusal(
            code="work_conflict",
            message=f"chain {chain!r} moved off {record['from']} while this adoption was recorded",
            remediation="Read GET /chains/{chain} again and adopt from the manifest it runs under now.",
            http_status=409,
        )
    events.append(
        conn, kind="ChainAdopted", actor=actor, agent_label=agent_label,
        occurred_at=record["adopted_at"],
        payload={"chain": chain, "from": record["from"], "to": to,
                 "changed": sum(1 for r in record["impact"] if r["effect"] != "unchanged")},
    )
    return {"state": "accepted", "adoption": record}


def pin_for_run(conn, chain: str, asop_id: str, version: Optional[int]) -> tuple[int, dict]:
    """The version a run of `asop_id` in `chain` must file at, and the pin to
    record on it. Resolved from the chain's CURRENT manifest only — no fallback
    to what is active now, which is the whole point of pinning (§3.10).

    A procedure the manifest does not pin, or a version other than the pinned
    one, is refused `adoption_required`."""
    _require_contract()
    current = _current(conn, chain)
    if current is None:
        raise _unknown_chain(chain)
    manifest = _manifest(conn, current)
    pinned = next((m["version"] for m in manifest["members"] if m["asop_id"] == asop_id), None)
    if pinned is None or (version is not None and not is_pinned(manifest, asop_id, version)):
        asked = f"v{version}" if version is not None else "any version"
        raise Refusal(
            code=ADOPTION_REQUIRED,
            message=(
                f"chain {chain!r} runs under {current}, which pins "
                f"{asop_id + ' v' + str(pinned) if pinned else 'no version of ' + asop_id}; "
                f"this run asked for {asked}"
            ),
            remediation=(
                "File at the pinned version, or have a person adopt a manifest that "
                "pins the one you need (POST /chains/{chain}/adoptions)."
            ),
            http_status=409,
        )
    return pinned, {"chain": chain, "manifest": current}


# --------------------------------------------------------------------------- #
# Approvals
# --------------------------------------------------------------------------- #


def _name(value: Any, what: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{what} must be a non-empty string with no leading or trailing space")
    return value


def validate_approval(doc: Mapping) -> dict:
    """`asop.approval/1` as its schema states it. The contract ships the schema
    and no validator for this one record, so the plane carries the check —
    exactly the schema's rules, nothing added."""
    allowed = {"schema", "artefact", "digest", "subset", "approved_by", "approved_at"}
    extra = set(doc) - allowed
    if extra:
        raise ValueError(f"approval: unknown field(s) {', '.join(sorted(map(str, extra)))}")
    if doc.get("schema") != APPROVAL_SCHEMA:
        raise ValueError(f"an approval declares schema {APPROVAL_SCHEMA!r}, got {doc.get('schema')!r}")
    out = {
        "schema": APPROVAL_SCHEMA,
        "artefact": _name(doc.get("artefact"), "'artefact'"),
        "digest": doc.get("digest"),
        "approved_by": _name(doc.get("approved_by"), "'approved_by'"),
        "approved_at": _name(doc.get("approved_at"), "'approved_at'"),
    }
    if not isinstance(out["digest"], str) or not out["digest"].strip():
        raise ValueError("'digest' must be the non-empty digest of exactly what was approved")
    if doc.get("subset") is not None:
        subset = doc["subset"]
        if isinstance(subset, (str, dict)) or not isinstance(subset, (list, tuple)):
            raise ValueError("'subset' must be a LIST of subject ids")
        for s in subset:
            if isinstance(s, bool) or not isinstance(s, (str, int)):
                raise ValueError(f"'subset' holds {s!r}; a subject id is a string or an integer")
            if isinstance(s, str):
                _name(s, "a 'subset' entry")
        if len(set(subset)) != len(subset):
            raise ValueError("'subset' names a subject twice")
        out["subset"] = list(subset)
    return out


def record_approval(conn, *, actor: str, humans: Iterable[str], artefact: Any, digest: Any,
                    subset: Any = None, agent_label: Optional[str] = None) -> dict:
    """Record that a declared human approved `artefact` at `digest`.

    Append-only: a later approval of the same artefact does not replace an
    earlier one, because "what was approved, and when" is the record a stale
    build is judged against (§5.7). `approved_by` and `approved_at` are the
    signature and the clock, never the body.
    """
    _require_contract()
    if policy.kind_of(actor, frozenset(humans)) != policy.HUMAN:
        raise human_required(
            f"{actor!r} is not a declared human; an approval is a person's",
            "Have a person the operator declared in AGENTCO_HUMANS record it.",
        )
    record = {"schema": APPROVAL_SCHEMA, "artefact": artefact, "digest": digest,
              "approved_by": actor, "approved_at": _now()}
    if subset is not None:
        record["subset"] = subset
    try:
        record = validate_approval(record)
    except ValueError as exc:
        raise _invalid("approval_invalid", exc) from exc
    uid = f"appr_{uuid.uuid4().hex[:16]}"
    with conn:
        conn.execute(
            "INSERT INTO approvals(uid, artefact, digest, doc, approved_by, approved_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (uid, record["artefact"], record["digest"], json.dumps(record, sort_keys=True),
             actor, record["approved_at"]),
        )
    events.append(
        conn, kind="ArtefactApproved", actor=actor, agent_label=agent_label,
        occurred_at=record["approved_at"],
        payload={"approvalId": uid, "artefact": record["artefact"], "digest": record["digest"],
                 "subset": record.get("subset")},
    )
    return {"state": "accepted", "approvalId": uid, "approval": record}


def approvals_of(conn, artefact: str, digest: Optional[str] = None) -> dict:
    """Every approval of `artefact`, oldest first, and the latest one. With
    `digest`, also whether the latest approval binds THAT digest — the check a
    `binds_digest` requirement makes (§3.7), answered by the store."""
    rows = conn.execute(
        "SELECT uid, doc FROM approvals WHERE artefact = ? ORDER BY approved_at, uid",
        (artefact,),
    ).fetchall()
    history = [{"approvalId": r["uid"], **json.loads(r["doc"])} for r in rows]
    latest = history[-1] if history else None
    out: dict = {"artefact": artefact, "latest": latest, "history": history}
    if digest is not None:
        out["digest"] = digest
        out["current"] = latest is not None and latest["digest"] == digest
    return out


__all__ = [
    "APPROVAL_SCHEMA",
    "CHAIN_KEY",
    "SUPPORTED",
    "adopt",
    "approvals_of",
    "chain_view",
    "impact",
    "pin_for_run",
    "record_approval",
    "record_manifest",
    "store_digest",
    "validate_approval",
]
