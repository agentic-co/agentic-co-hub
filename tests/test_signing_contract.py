"""The two signing implementations agree, on every shape either will meet.

`publish.py` keeps a standard-library-only copy of the signer on purpose — it is
meant to be copied next to whatever a team already runs, rather than installed.
Its own comment names the risk that creates: "a drifted copy does not fail at
import. It produces valid-looking signatures the server rejects, so a
colleague's first contact with the registry is a 401 they cannot debug." And it
notes that two docstrings once claimed this file imported the function, each
pointing at the other as the safeguard.

This is the safeguard. It would have failed the moment the query joined the
signed string in one implementation and not the other.
"""

from __future__ import annotations

import pytest

from agentco import auth, publish

SECRET = "a-shared-secret"

CASES = [
    ("GET", "/events", "", b""),
    ("GET", "/events", "?limit=50", b""),
    ("GET", "/events", "?since=evt_00000042&limit=50", b""),
    ("GET", "/events", "?limit=50&since=evt_00000042", b""),          # reordered
    ("GET", "/events", "?kind=AsopActivated&limit=1&since=", b""),     # blank value
    ("GET", "/sops/asop-1", "?version=2", b""),
    ("POST", "/work", "", b'{"title":"x"}'),
    ("POST", "/work/pull", "", b"{}"),
    ("POST", "/scope-claims", "", b'{"repo":"acme/web","prefixes":["src/billing/"]}'),
    ("GET", "/events", "?q=a%20b", b""),                               # encoded value
    ("GET", "/events", "?q=a+b", b""),
    ("GET", "/events", "?a=1&a=2", b""),                               # repeated key
]


@pytest.mark.parametrize("method,path,query,body", CASES,
                         ids=[f"{m} {p}{q or ''}" for m, p, q, body in CASES])
def test_both_implementations_produce_one_signature(method, path, query, body):
    assert auth.sign(SECRET, method, path, "1758000000", body, query=query) == \
        publish._sign(SECRET, method, path, "1758000000", body, query=query)


def test_reordering_parameters_does_not_change_the_signature():
    """The property the query was left out of the signature to protect. It is
    kept by canonicalising rather than by not signing."""
    a = auth.sign(SECRET, "GET", "/events", "1758000000", b"", query="?since=c1&limit=50")
    b = auth.sign(SECRET, "GET", "/events", "1758000000", b"", query="?limit=50&since=c1")
    assert a == b


def test_changing_a_parameter_value_does_change_it():
    """And the property that was missing: the values are bound now."""
    a = auth.sign(SECRET, "GET", "/events", "1758000000", b"", query="?since=c1")
    b = auth.sign(SECRET, "GET", "/events", "1758000000", b"", query="?since=c2")
    assert a != b


def test_a_request_with_no_query_signs_exactly_as_it_always_did():
    """Which is what lets the old and new forms coexist without a version field,
    and what keeps every existing body-carrying client working untouched."""
    import hashlib

    body = b'{"title":"x"}'
    legacy = f"POST\n/work\n1758000000\n{hashlib.sha256(body).hexdigest()}"
    assert auth.signing_string("POST", "/work", "1758000000", body) == legacy
    assert auth.signing_string("POST", "/work", "1758000000", body, query="") == legacy
