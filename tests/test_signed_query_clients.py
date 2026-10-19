"""Every client this repo ships signs the query, so a hub with
`ASOP_REQUIRE_SIGNED_QUERY=1` (ADR 0008 §6) refuses none of them.

`Registry._call` always did; the conformance loopback client and the load-test
actor signed the path alone, and were documented as unusable against such a hub.
"""

from __future__ import annotations

import importlib.util
import socket
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agentco import conformance
from agentco.app import create_app

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _signed_queries_required(monkeypatch):
    monkeypatch.setenv("ASOP_REQUIRE_SIGNED_QUERY", "1")


def make_app(tmp_path):
    return create_app(db_path=str(tmp_path / "api.sqlite3"), keys=dict(conformance.KEYS), operator="operator",
                      work_store=str(tmp_path / "work.jsonl"), sop_store=str(tmp_path / "sops.jsonl"))


def test_the_conformance_client_signs_its_query(tmp_path):
    actor = sorted(conformance.KEYS)[0]
    client = conformance._LoopbackRegistry(actor, TestClient(make_app(tmp_path)))
    page = client._call("GET", "/events", query="?kind=WorkFiled&limit=5")
    assert page["events"] == []


@pytest.mark.parametrize("transport", ["http", "mcp-remote", "outbox"])
def test_the_conformance_scenarios_pass_with_signed_queries_required(transport):
    for name in sorted(conformance.SCENARIOS):
        result = conformance.compare(name, transports=(transport,))["transports"][transport]
        assert result["conforms"], f"{name}: " + "\n".join(result["diffs"])


def test_the_load_test_actor_signs_its_query(tmp_path):
    import uvicorn

    spec = importlib.util.spec_from_file_location("loadtest", ROOT / "tools" / "loadtest.py")
    loadtest = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loadtest)
    actor = sorted(conformance.KEYS)[0]
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = uvicorn.Server(uvicorn.Config(make_app(tmp_path), host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    try:
        results = loadtest.Results()
        load = loadtest.Actor(actor, conformance.KEYS[actor], f"http://127.0.0.1:{port}", 1, time.time() + 5,
                              results, 0.0)
        assert load._call("GET", "/events?limit=5") is not None, "a signed-query hub refused the load-test actor"
    finally:
        server.should_exit = True
        thread.join(timeout=5)
