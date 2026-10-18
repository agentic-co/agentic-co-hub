"""The remote MCP relay honours the `events` tool's `kind` filter (found by the live wire probes).

The `events` tool takes `kind`, the local backend and `GET /events?kind=` both
filter by it, and the remote backend dropped it: a harness asking a shared
registry for `WorkFiled` only got every kind on the feed, with nothing to say
the filter had not been applied.
"""

from __future__ import annotations

from tests.test_mcp_remote import KEYS, LoopbackRegistry, client, filer, remote, tool  # noqa: F401


def test_kind_filters_the_feed_through_the_remote_relay(remote, filer):
    tool(filer, "claim_scope")(repo="acme/web", prefixes=["src/billing/invoices"], intent="implement")
    tool(filer, "work_create")(title="rebuild the exporter")
    page = tool(remote, "events")(kind="WorkFiled")
    assert page["events"], "the filtered kind is present"
    assert {e["kind"] for e in page["events"]} == {"WorkFiled"}


def test_registry_events_sends_kind():
    sent = {}

    class Capture(LoopbackRegistry):
        def __init__(self):
            pass

        def _call(self, method, path, body=None, query=""):
            sent.update(method=method, path=path, query=query)
            return {"events": [], "nextCursor": "v1:0"}

    Capture().events(kind="WorkFiled")
    assert "kind=WorkFiled" in sent["query"]
