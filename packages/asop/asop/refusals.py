"""The refusal VOCABULARY — every machine-readable code a `Refusal` carries
across the plane's own modules, named once with a one-line meaning.

This is a catalogue, not a classifier. A caller — plane or harness — that
needs to turn an arbitrary internal exception into a `Refusal` still writes
its own `classify(exc)`, because that mapping is from PLANE-INTERNAL
exception *classes* (`WorkError`, `SopError`, `RevisionPolicyError`, …) to a
code, and those classes are not part of this contract; only the codes are.
`agentco/refusals.py` keeps its `classify()` and imports the codes it
assigns from here, so a code is spelled once even though it is assigned in
one place and read in several.

Codes are grouped by the concern that raises them. Most of this vocabulary —
the ADO connector, the routing table, scope claims — is plane-specific: an
AgentCo Harness node never sees an `ado_pat_missing` refusal, because it
never calls the plane's ADO connector. It is catalogued here anyway, for the
same reason the whole vocabulary lives in one file in the plane today: a
refusal is a wire-visible fact about what a caller can expect back, and
"what codes exist and what each one means" is one question with one honest
answer, not one answer per module.

The GATE_* and ATTESTATION_* codes are the ones actually shared with a
harness — they are also exported directly from `asop.gates`, which is the
module that raises them; this table repeats their meaning for the reader
who is scanning codes rather than gates.py.
"""

from __future__ import annotations

from asop.gates import ATTESTATION_INVALID, ATTESTATION_REQUIRED, GATE_INVALID

# code -> one-line meaning. Values are descriptions, not remediation text —
# the remediation is written fresh at each call site because it depends on
# the specific payload that triggered it; this table answers "what does this
# code mean", not "what do I do about it".
CODES: dict[str, str] = {
    # -- the ASOP gate/attestation contract (asop.gates) -------------------
    GATE_INVALID: "a gate payload failed validation — malformed, an unknown field, or a field that contradicts another one (asop.gates.validate_gate)",
    ATTESTATION_INVALID: "an attestation payload failed validation, or attests to a different check than the gate declares (asop.gates.validate_attestation)",
    ATTESTATION_REQUIRED: "a DONE report on a deterministic gate arrived with no attestation attached",
    # -- auth (agentco/auth.py) ---------------------------------------------
    "unauthenticated": "the request carried no valid signature for the actor it claims to be",
    "bad_agent_label": "an unauthenticated agent-supplied label failed its own (looser) shape check",
    "actor_in_body": "the request body named an actor — the actor comes from the authenticated token, never the payload",
    # -- work queue (agentco/work.py) ---------------------------------------
    "not_terminal": "a status transition targets something other than a terminal status where only a terminal one is accepted",
    "work_item_unknown": "no work item exists with the given id",
    "metadata_reserved": "the caller tried to set a metadata key the queue reserves for its own bookkeeping",
    "natural_key_reserved": "a natural_key value collides with a prefix the queue reserves internally",
    "work_conflict": "the request was well-formed and lost to the state of the world (a lease race, a blocked dependency)",
    "decomposition_bound": "a create/repair call named a tree position (depth, child count) this item may not take",
    "capability_mismatch": "the caller does not hold a capability (e.g. 'verify') the operation requires",
    "adjudication_invalid": "an adjudication payload failed validation",
    "adjudication_self": "the adjudicator and the executor are the same actor — adjudicator must differ from executor",
    "adjudication_exists": "this item already carries a terminal adjudication; adjudication is write-once",
    "adjudication_unexecuted": "an adjudication was attempted on an item that never reported a terminal outcome",
    # -- revision policy (agentco/policy.py, via agentco/refusals.py) -------
    "revision_policy:<rule>": "an SOP revision violated one of the three revision-policy rules for agent revisers; the actual code carries the rule name after the colon",
    # -- SOP store (agentco/sop.py) ------------------------------------------
    "sop_refused": "an SOP-store operation was refused (SopError not otherwise classified)",
    "version_required": "an operation needed a specific SOP version and none (or an invalid one) was given",
    # -- scope claims (agentco/scope.py) -------------------------------------
    "scope_too_broad": "a claimed path prefix names fewer directory segments than the registry's minimum specificity",
    "scope_escapes_repo": "a claimed path prefix contains a '..' segment that would leave the repo root",
    "unknown_intent": "a scope claim's intent is not one of the registry's known intents",
    "control_character": "a string field contains a raw control character",
    "repo_required": "a scope claim named no repo",
    "scope_required": "a scope claim named no path prefixes",
    # -- snapshots (agentco/snapshots.py) ------------------------------------
    "bad_uri": "a snapshot URI has no recognisable scheme",
    "purpose_required": "a snapshot call named no purpose",
    # -- the change feed (agentco/events.py) ---------------------------------
    "bad_cursor": "an `events` cursor failed to decode, or does not carry the expected prefix",
    # -- leases (agentco/leases.py) ------------------------------------------
    "bad_ttl": "a lease TTL is not a positive integer within the registry's bound",
    "no_such_lease": "no lease exists for the given key",
    "not_the_holder": "the caller does not hold the lease it tried to act on",
    # -- HTTP surface (agentco/app.py) ---------------------------------------
    "not_an_integer": "a query or body field expected to be an integer was not one",
    "bad_json": "the request body is not valid JSON, or not a JSON object",
    "attempt_required": "a fenced write needs the lease attempt it is conditioned on, and none was given",
    "adjudicator_from_signature": "the request body tried to name an adjudicator other than the authenticated actor",
    "author_from_signature": "the request body tried to name an author other than the authenticated actor",
    "unknown_status": "a status value in the request body is not one the queue recognises",
    # -- MCP server (agentco/mcp_server.py) ----------------------------------
    "secret_required": "the MCP server was started without a signing secret it needs for the actor it is configured as",
    # -- routing table (agentco/routing.py) ----------------------------------
    "route_sop_unknown": "a routing rule names an SOP id the library does not hold",
    "routes_missing": "the routing table file does not exist",
    "routes_bad_json": "the routing table file is not valid JSON",
    "routes_no_sops": "the routing table declares no rules at all",
    "routes_bad_default": "the routing table's default SOP id is not one of its declared SOPs",
    "routes_unknown_predicate": "a routing rule uses a predicate the router does not implement",
    "routes_empty_rule": "a routing rule matches nothing — a rule that can never fire",
    # -- the outbox / L1 (agentco/outbox.py) ---------------------------------
    "outbox_line_invalid": "a line appended to .agentco/outbox.jsonl failed shape validation and the drainer refused to sign it",
    # -- the ADO connector (agentco/ado.py) — plane-specific, configuration-bound
    "ado_http_error": "Azure DevOps returned a non-2xx response to a connector call",
    "ado_unreachable": "the connector could not reach the configured Azure DevOps endpoint at all",
    "ado_pat_missing": "the connector needs an Azure DevOps PAT and none is configured",
    "connector_missing": "a connector's config file does not exist",
    "connector_bad_json": "a connector's config file is not valid JSON",
    "connector_incomplete": "a connector's config file is missing required keys",
    "connector_bad_types": "a connector's config declares work-item types in a shape the connector does not accept",
    "connector_bad_tags": "a connector's config declares tags in a shape the connector does not accept",
    "ado_bad_filter": "a connector filter value contains a newline, which would break the query it is interpolated into",
    # -- the generic fallback (agentco/refusals.py:classify) ------------------
    "invalid_request": "an exception the plane's classifier did not recognise as any of the above — the fallback of last resort",
    "registry_<status>": "a Refusal that already crossed the wire once (a remote registry call proxied through MCP) is passed through with its own status; the actual code is 'registry_' followed by the HTTP status, or 'registry_error' if none was given",
    "natural_key_invalid": "a natural_key value failed its own shape check",
    # -- ASOP v3: filing a run (ASOP.md §5.1) ------------------------------
    "inputs_missing": "a run was filed without an input the ASOP declares by name",
    "role_unbound": "a run was filed with no binding for a role the ASOP declares",
    "constraint_unsatisfiable": "every binding offered for a run violates a `distinct` constraint — the same binding would fill both roles",
}
