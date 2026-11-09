"""Snapshot URI resolution must honour admission on every hop and every input.

Each test names the property that should hold and began life
`xfail(strict=True)` against the pre-fix code (see the docstring of
`tests/test_adversarial_findings.py` for why). Closes known-issues Finding 5
(redirect bypasses admission) and A16 (FIFO hang, directory crash).

Hermetic: servers are in-process on loopback. The "public" starting host is
admitted through the operator allowlist (`ASOP_SNAPSHOT_HTTP_HOSTS`), which is
the only way a loopback listener can pass admission; the redirect target is
spelled differently (`localhost`) so it is NOT allowlisted and must be refused.
"""

from __future__ import annotations

import http.server
import os
import socket
import threading

import pytest

from agentco import snapshots


class _Server:
    def __init__(self, handler_factory):
        self.hits: list[str] = []
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_HEAD(self):  # noqa: N802
                outer.hits.append(self.path)
                handler_factory(self)

            def log_message(self, *a):
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def _etag(h):
    h.send_response(200)
    h.send_header("ETag", '"v1"')
    h.end_headers()


def _redirector(location):
    def f(h):
        h.send_response(302)
        h.send_header("Location", location)
        h.end_headers()

    return f


@pytest.fixture
def servers():
    made: list[_Server] = []

    def make(factory):
        s = _Server(factory)
        made.append(s)
        return s

    yield make
    for s in made:
        s.close()


@pytest.fixture(autouse=True)
def _allow_loopback_entry_host(monkeypatch):
    monkeypatch.setenv(snapshots.HTTP_HOSTS_ENV_VAR, "127.0.0.1")


def test_redirect_to_a_refused_address_is_never_requested_and_is_unresolved(servers):
    internal = servers(_etag)
    entry = servers(_redirector(f"http://localhost:{internal.port}/secret"))
    kind, value, reason = snapshots.resolve(f"http://127.0.0.1:{entry.port}/doc")
    assert internal.hits == [], "the redirect target was requested despite admission"
    assert (kind, value) == (None, None)
    assert reason and "Recorded, not followed" in reason


def test_redirect_between_admitted_hosts_is_still_followed(servers):
    final = servers(_etag)
    entry = servers(_redirector(f"http://127.0.0.1:{final.port}/real"))
    kind, value, reason = snapshots.resolve(f"http://127.0.0.1:{entry.port}/doc")
    assert (kind, value, reason) == ("etag", '"v1"', None)
    assert final.hits == ["/real"]


def test_connection_goes_to_an_address_that_passed_the_check_not_a_rebound_one(
    servers, monkeypatch
):
    """DNS rebinding: admission sees a public address, a later lookup says loopback."""
    internal = servers(_etag)
    monkeypatch.delenv(snapshots.HTTP_HOSTS_ENV_VAR)
    real = socket.getaddrinfo
    calls = {"n": 0}

    def rebinding(host, port=None, *a, **kw):
        if host == "rebind.test":
            calls["n"] += 1
            addr = "93.184.216.34"  # leakguard: allow — a public address for the test if calls["n"] == 1 else "127.0.0.1"
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (addr, port or 0))]
        return real(host, port, *a, **kw)

    monkeypatch.setattr(socket, "getaddrinfo", rebinding)
    _, _, reason = snapshots.resolve(f"http://rebind.test:{internal.port}/x")
    assert internal.hits == [], "connected to the rebound loopback address"
    assert reason


def test_file_uri_naming_a_fifo_is_unresolved_instead_of_blocking(tmp_path, monkeypatch):
    if not hasattr(os, "mkfifo"):
        pytest.skip("no os.mkfifo on this platform")
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    monkeypatch.setenv(snapshots.SCHEMES_ENV_VAR, "file")
    monkeypatch.setenv(snapshots.FILE_ROOTS_ENV_VAR, str(tmp_path))
    out: list = []
    t = threading.Thread(
        target=lambda: out.append(snapshots.resolve(f"file:{fifo}")), daemon=True
    )
    t.start()
    t.join(5)
    if t.is_alive():
        # Release the blocked open() so the daemon thread cannot outlive the test.
        fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
        os.close(fd)
        t.join(2)
        pytest.fail("resolve() on a FIFO blocked")
    kind, value, reason = out[0]
    assert (kind, value) == (None, None) and reason


def test_file_resolver_on_a_directory_reports_unresolved_like_a_missing_file(tmp_path):
    # resolve() happens to absorb the raw IsADirectoryError as "unreachable"; the
    # resolver itself must speak the same ResolverError a missing file does.
    with pytest.raises(snapshots.ResolverError, match="not a regular file"):
        snapshots.resolve_file(f"file:{tmp_path}")
