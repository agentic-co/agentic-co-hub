"""A snapshot's TTL survives every transport (found by the live wire probes).

`snapshots.take` takes `ttl_days` and the local MCP `snapshot` tool forwards it,
but `POST /snapshots` never read `ttlDays`, `Registry.snapshot` had no way to
send it, and the remote MCP backend dropped `ttl_days` on the floor. The same
call, `ttl_days=2`, expired in 2 days locally and in 90 over HTTP — with a 200.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from tests.test_mcp_remote import KEYS, LoopbackRegistry, client, remote, tool  # noqa: F401


def expires_in_days(receipt) -> float:
    at = datetime.fromisoformat(receipt["expiresAt"])
    return (at - datetime.now(timezone.utc)) / timedelta(days=1)


def test_http_honours_ttl_days(client):
    receipt = LoopbackRegistry("macbook", client).snapshot("file:///nonexistent/a.md", "probe", ttl_days=2)
    assert 1.9 < expires_in_days(receipt) < 2.1


def test_the_remote_mcp_relay_honours_ttl_days(remote):
    receipt = tool(remote, "snapshot")(artifact_uri="file:///nonexistent/b.md", purpose="probe", ttl_days=3)
    assert 2.9 < expires_in_days(receipt) < 3.1


def test_no_ttl_keeps_the_default(client):
    from agentco import snapshots

    receipt = LoopbackRegistry("macbook", client).snapshot("file:///nonexistent/c.md", "probe")
    assert abs(expires_in_days(receipt) - snapshots.DEFAULT_TTL_DAYS) < 0.1
