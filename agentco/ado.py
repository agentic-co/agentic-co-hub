"""Azure DevOps as a work source — read-only, pointer-shaped, idempotent.

An organisation's work already lives somewhere. AgentCo's first principle is
that it never becomes a second home for it: *"never hold a competing version of
a fact another system owns."* So this adapter does the least it can and says so
in its shape:

  * **It reads. It never writes.** Nothing here issues a PATCH, a POST to a
    work item, or a comment. A PAT with write scope is a common thing to have
    lying around, and an ingest that could write is one bug away from editing
    somebody's backlog.
  * **It carries a pointer, not a copy.** Title, type, state and URL — enough
    for an agent to know what it is looking at and where to go for the truth.
    Not the description, not the comments, not the attachments. A cached
    description is a competing version of the fact ADO owns.
  * **Re-running it is a no-op.** Every item is filed under the natural key
    `ado` + `{org}/{id}`, so the queue's duplicate rule makes a second pull
    return the existing item rather than filing it twice. That is what makes
    this safe to put on a schedule.

**Nothing here names an organisation.** Org URL, project and query are
arguments; the PAT is read from an env var whose name is also an argument. The
adapter is code, the company is configuration — which is the rule that makes
this repo publishable at all (CONTRIBUTING.md).

The fetcher is injected rather than constructed inside the query functions, so
the tests exercise the real mapping and paging logic against recorded shapes
with no network and no credentials.
"""

from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from agentco.errors import Refusal

API_VERSION = "7.0"
DEFAULT_PAT_ENV_VAR = "AGENTCO_ADO_PAT"

# One batch call has a server-side ceiling. Exceeding it fails the whole
# request rather than truncating, so the chunking is not an optimisation.
MAX_IDS_PER_BATCH = 200

# The only fields that cross the boundary. Deliberately short: each addition is
# another fact this system would be caching on ADO's behalf.
FIELDS = (
    "System.Id",
    "System.Title",
    "System.WorkItemType",
    "System.State",
    "System.TeamProject",
)

Fetcher = Callable[..., dict]


def make_fetcher(pat: str, timeout: int = 30) -> Fetcher:
    """Basic auth with an EMPTY username — that is how ADO takes a PAT.

    The token never appears in a URL, an argument list or a log line: it is
    built into a header here and nowhere else, because a PAT in `ps` output or
    a shell history is a PAT that has leaked.
    """
    token = base64.b64encode(f":{pat}".encode()).decode()

    def fetch(url: str, body: Optional[dict] = None) -> dict:
        raw = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(
            url,
            data=raw,
            method="POST" if body is not None else "GET",
            headers={
                "Authorization": f"Basic {token}",
                "Accept": "application/json",
                **({"Content-Type": "application/json"} if body is not None else {}),
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode()[:400]
            raise Refusal(
                code="ado_http_error",
                message=f"Azure DevOps answered {exc.code} for {url}",
                remediation=(
                    "A 203 or an HTML body almost always means the PAT is wrong, expired, "
                    "or lacks Work Items (Read) on this organisation — ADO answers an "
                    f"unauthenticated API call with a sign-in page, not a 401. Body: {detail}"
                ),
                http_status=502,
            ) from exc
        except urllib.error.URLError as exc:
            raise Refusal(
                code="ado_unreachable",
                message=f"cannot reach {url}: {exc.reason}",
                remediation="Check the org URL and that this machine can reach it.",
                http_status=502,
            ) from exc

    return fetch


def resolve_pat(env_var: str = DEFAULT_PAT_ENV_VAR) -> str:
    pat = os.environ.get(env_var)
    if not pat:
        raise Refusal(
            code="ado_pat_missing",
            message=f"{env_var} is not set",
            remediation=(
                f"Export {env_var} with a PAT that has Work Items (Read). "
                "Read is the only scope this adapter uses."
            ),
            http_status=400,
        )
    return pat


def _url(org_url: str, *segments: str, **params: str) -> str:
    base = org_url.rstrip("/")
    path = "/".join(urllib.parse.quote(str(s), safe="") for s in segments)
    query = {"api-version": API_VERSION, **{k: v for k, v in params.items() if v}}
    return f"{base}/{path}?{urllib.parse.urlencode(query)}"


def wiql_ids(fetch: Fetcher, org_url: str, project: str, wiql: str, limit: int = 50) -> list[int]:
    """Run a WIQL query and return the matching ids, newest first.

    WIQL is a POST that reads. That is ADO's design, not a write: the query is
    the body because it does not fit in a query string. No work item is touched.
    """
    result = fetch(_url(org_url, project, "_apis", "wit", "wiql"), {"query": wiql})
    return [int(row["id"]) for row in result.get("workItems", [])][:limit]


def fetch_items(fetch: Fetcher, org_url: str, ids: Iterable[int]) -> list[dict]:
    """Batch-read the few fields that cross the boundary."""
    ids = list(ids)
    out: list[dict] = []
    for start in range(0, len(ids), MAX_IDS_PER_BATCH):
        chunk = ids[start:start + MAX_IDS_PER_BATCH]
        if not chunk:
            continue
        result = fetch(
            _url(
                org_url,
                "_apis", "wit", "workitems",
                ids=",".join(str(i) for i in chunk),
                fields=",".join(FIELDS),
            )
        )
        out.extend(result.get("value", []))
    return out


def org_name(org_url: str) -> str:
    """The last path segment — the org's own name for itself, used in the key."""
    return org_url.rstrip("/").rsplit("/", 1)[-1]


def to_work_payload(raw: dict, org_url: str, assign: Optional[str] = None) -> dict:
    """One ADO work item as the arguments `work_create` takes.

    The natural key is `source` + `sourceId`, which is what makes a repeated
    pull a no-op rather than a duplicate. `sourceId` is org-qualified because
    ADO ids are unique per organisation, not globally — two orgs both having a
    work item 41234 is ordinary, and silently merging them would be worse than
    filing nothing.
    """
    fields = raw.get("fields") or {}
    item_id = raw.get("id")
    project = fields.get("System.TeamProject", "")
    item_type = fields.get("System.WorkItemType", "Work Item")
    return {
        "title": f"[{item_type} {item_id}] {fields.get('System.Title', '').strip()}",
        "source": "ado",
        "sourceId": f"{org_name(org_url)}/{item_id}",
        "assignedAgent": assign,
        "metadata": {
            # The pointer. An agent that needs the description, the comments or
            # the attachments goes here — this system does not hold them.
            "url": f"{org_url.rstrip('/')}/{urllib.parse.quote(project)}/_workitems/edit/{item_id}",
            "adoId": item_id,
            "adoType": item_type,
            "adoState": fields.get("System.State"),
            "adoProject": project,
        },
    }


def route_view(payload: dict) -> dict:
    """The few facts a routing rule may see, projected out of one payload.

    Narrow on purpose. A rule that could match on anything in the item would
    couple one organisation's routing to this adapter's internal shape, and
    every field added here becomes a field the rules file may come to depend on.
    """
    meta = payload.get("metadata") or {}
    return {
        "title": payload.get("title", ""),
        "type": meta.get("adoType"),
        "state": meta.get("adoState"),
        "id": meta.get("adoId"),
    }


DEFAULT_STATES = ("New", "Active", "To Do")


@dataclass(frozen=True)
class Connector:
    """One organisation's Azure DevOps, as configuration.

    `types` is the one that changes what work *means*. Azure DevOps nests Epic
    above Feature above User Story, and picking a level is picking the size of
    the thing an agent is handed. An Epic claimed as one work item is a whole
    programme claimed as one work item — the lease is real, the fence is real,
    and the unit is nonsense. Which level is right is a fact about how a team
    writes its backlog, so it lives here rather than in a constant.
    """

    org_url: str
    project: str
    types: tuple[str, ...] = ()
    states: tuple[str, ...] = DEFAULT_STATES
    contains: Optional[str] = None
    wiql: Optional[str] = None
    limit: int = 50
    pat_env: str = DEFAULT_PAT_ENV_VAR


def load_connector(path: str | Path) -> Connector:
    """Read and validate a connector file. Every refusal names the fix."""
    try:
        raw = json.loads(Path(path).read_text())
    except FileNotFoundError:
        raise Refusal(
            code="connector_missing",
            message=f"no connector file at {path}",
            remediation='Point --connector at a JSON file with at least {"orgUrl": …, "project": …}.',
            http_status=400,
        ) from None
    except json.JSONDecodeError as exc:
        raise Refusal(
            code="connector_bad_json",
            message=f"{path} is not valid JSON: {exc}",
            remediation="Fix the JSON — this file decides which work is pulled at all.",
            http_status=400,
        ) from None

    missing = [k for k in ("orgUrl", "project") if not raw.get(k)]
    if missing:
        raise Refusal(
            code="connector_incomplete",
            message=f"connector file is missing: {', '.join(missing)}",
            remediation='Both are required: {"orgUrl": "https://dev.azure.com/<org>", "project": "<project>"}.',
            http_status=400,
        )

    types = tuple(raw.get("types") or ())
    if any(not str(t).strip() for t in types):
        raise Refusal(
            code="connector_bad_types",
            message="`types` contains a blank entry",
            remediation=(
                'List the work item types to pull, e.g. ["Feature"]. An empty '
                "entry would widen the query rather than narrow it, which is the "
                "opposite of what a filter is for."
            ),
            http_status=400,
        )
    return Connector(
        org_url=raw["orgUrl"],
        project=raw["project"],
        types=types,
        states=tuple(raw.get("states") or DEFAULT_STATES),
        contains=raw.get("contains"),
        wiql=raw.get("wiql"),
        limit=int(raw.get("limit", 50)),
        pat_env=raw.get("patEnv") or DEFAULT_PAT_ENV_VAR,
    )


def build_wiql(
    project: str,
    contains: Optional[str] = None,
    types: Iterable[str] = (),
    states: Iterable[str] = DEFAULT_STATES,
) -> str:
    """A conservative default query: open work in one project, optionally filtered.

    Closed and Removed items are excluded rather than filtered afterwards — a
    pull that files finished work as pending is how a queue fills with things
    nobody should do.

    The type filter is in the QUERY and not applied to the results, which is not
    a micro-optimisation: `--limit` bounds what the query returns, so filtering
    afterwards means asking for twenty items, being handed twenty Epics, and
    filing nothing while reporting success.
    """
    clauses = [f"[System.TeamProject] = '{_escape(project)}'"]
    state_list = ", ".join(f"'{_escape(s)}'" for s in states)
    clauses.append(f"[System.State] IN ({state_list})")
    types = tuple(types)
    if types:
        type_list = ", ".join(f"'{_escape(t)}'" for t in types)
        clauses.append(f"[System.WorkItemType] IN ({type_list})")
    if contains:
        clauses.append(f"[System.Title] CONTAINS '{_escape(contains)}'")
    return (
        "SELECT [System.Id] FROM WorkItems WHERE "
        + " AND ".join(clauses)
        + " ORDER BY [System.ChangedDate] DESC"
    )


def _escape(value: str) -> str:
    """WIQL string literals are single-quoted; a quote is doubled.

    Refused rather than stripped if it contains a newline: a multi-line value
    reaching a query builder is a sign the caller is passing something other
    than a name, and silently repairing it would hide that.
    """
    if "\n" in value or "\r" in value:
        raise Refusal(
            code="ado_bad_filter",
            message="a WIQL filter value may not contain a newline",
            remediation="Pass a single-line value for --contains / --type / --project.",
            http_status=400,
        )
    return value.replace("'", "''")


def pull(
    fetch: Fetcher,
    connector: Connector,
    *,
    ids: Optional[Iterable[int]] = None,
    assign: Optional[str] = None,
) -> tuple[list[dict], list[dict]]:
    """Resolve a query to work payloads. Files nothing — the caller decides that.

    Returns `(payloads, dropped)`. Splitting the read from the write is what
    lets dry-run be the default and still show exactly what would be filed
    rather than a description of what it might be; returning the drops is what
    stops a type filter from being a silent truncation. A pull that quietly
    discards half its results reads identically to one that found half as much.
    """
    if ids is None:
        query = connector.wiql or build_wiql(
            connector.project, connector.contains, connector.types, connector.states
        )
        ids = wiql_ids(fetch, connector.org_url, connector.project, query, connector.limit)
    raw_items = fetch_items(fetch, connector.org_url, ids)

    kept, dropped = [], []
    for raw in raw_items:
        payload = to_work_payload(raw, connector.org_url, assign)
        item_type = (raw.get("fields") or {}).get("System.WorkItemType")
        # Explicit ids and a raw WIQL both bypass the built query, so the type
        # filter is re-applied here. Naming an id is not a licence to file an
        # Epic into a queue configured for Features — but it is also not
        # something to swallow, so the drop is returned and reported.
        if connector.types and item_type not in connector.types:
            dropped.append({**payload, "reason": f"type {item_type!r} is not in {list(connector.types)}"})
            continue
        kept.append(payload)
    return kept, dropped
