"""`POST /snapshots` — a pointer, never a copy.

A snapshot records *"I am working from this version of that"*: a URI plus a
cheap version token — a git SHA, a content hash, a document eTag or revision
number. **The body is never fetched, never copied, never stored.**

This is the invariant most worth defending in the whole system, because
violating it is how a coordination layer quietly becomes the document store it
promised not to be. There is no code path in this module that reads an
artifact's content into the database: the `file:` resolver hashes bytes on the
way past and keeps only the digest. `tests/test_registry.py` asserts this
adversarially, by scanning the database file's raw bytes for content that was
definitely in the artifact — not by checking the API, which an implementation
change could satisfy while still leaking.

**Resolution never blocks the write.** A scheme with no registered resolver is
still recorded, because the alternative — refusing the write — makes the most
valuable endpoint unreachable until whatever the resolver needs (a credential,
an API permission, an admin's approval) is in place. Those approvals can take
weeks, and a coordination tool that cannot be used until then will not be used
after.

But an unresolvable snapshot is recorded **loudly**, never silently. A pointer
whose version token cannot be read can never report divergence, and one that
will never fire while looking exactly like one that might is the worst shape
available: an absence of alarms that reads as an absence of problems. The
receipt says so in `freshness.externalResolution`, and the digest counts those
pointers above its content rather than in a footnote.

**Resolvers are pluggable.** Three ship here — `git:`, `file:` and `https:` —
because they need no credentials and work on any machine. Everything else is a
connector's job: a document-store or issue-tracker integration calls
`register_resolver()` at import time. That keeps this module free of any
particular vendor, which is the same rule the repository is published under.
"""

from __future__ import annotations

import hashlib
import sqlite3
import ipaddress
import os
import socket
import subprocess
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse

from agentco import events
from agentco.errors import Refusal

DEFAULT_TTL_DAYS = 90


class ResolverError(Exception):
    """A resolver reached its target and could not produce a version token.

    Distinct from "no resolver registered": this means the connector exists and
    the artifact is gone, moved, or unreadable. That is a real signal about the
    artifact, so it is surfaced rather than folded into the unsupported case.
    """


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def resolve_git(uri: str) -> tuple[str, str]:
    """`git:<repo-path>#<rev>` → ('git-sha', <full sha>). Metadata only."""
    parsed = urlparse(uri)
    target = parsed.path or parsed.netloc
    rev = parsed.fragment or "HEAD"
    result = subprocess.run(
        ["git", "-C", target, "rev-parse", rev],
        capture_output=True,
        text=True,
        timeout=15,
    )
    if result.returncode != 0:
        # ResolverError, not Refusal. A pointer whose target has MOVED is
        # exactly the case this module exists to record — refusing it means the
        # endpoint fails on the artifacts most worth tracking. Refusal is for a
        # request that was never well-formed; this request was fine and the
        # world changed.
        raise ResolverError(
            f"git could not resolve {rev!r} in {target!r} — the branch or "
            f"revision may have moved or the repo may not be there. Format is "
            f"'git:/abs/path/to/repo#branch-or-sha'."
        )
    return "git-sha", result.stdout.strip()


def resolve_file(uri: str) -> tuple[str, str]:
    """`file:<path>` → ('sha256', <digest>).

    Streams in 64 KiB chunks and keeps only the digest — the body is never
    held whole in memory and never reaches the database.
    """
    path = Path(urlparse(uri).path)
    if not path.exists():
        # ResolverError for the same reason as git above: an absent file is a
        # fact about the world, not a malformed request.
        raise ResolverError(
            f"no file at {path}. A file that exists only on one machine has no "
            f"stable pointer for anyone else, which is a documented limit rather "
            f"than a bug — snapshot the committed or published artifact instead."
        )
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return "sha256", digest.hexdigest()


def resolve_https(uri: str) -> tuple[str, str]:
    """`https://…` → ('etag', <etag>) via a HEAD request. Metadata only.

    HEAD rather than GET is the whole point: it returns the validator without
    transferring the body, so the "never copy" invariant holds even for a
    resolver pointed at a large document. A server that returns no `ETag` and
    no `Last-Modified` cannot be tracked, and says so rather than inventing a
    hash from content it should not have fetched.
    """
    import urllib.request

    class _KeepMethodOnRedirect(urllib.request.HTTPRedirectHandler):
        """Preserve HEAD across a 3xx. urllib does not, before Python 3.13.

        `HTTPRedirectHandler.redirect_request` rebuilds the request WITHOUT
        passing `method=`, so a redirected HEAD silently becomes a GET and the
        body transfers — defeating the entire reason this resolver uses HEAD.
        Redirects are the norm for the document stores this points at, so it was
        the common path rather than an edge case.

        CPython fixed this in 3.13, which is how it was found: the failing test
        XPASSed on one row of the version matrix and not the others. A defect
        that exists on two of three supported interpreters is still a defect, and
        pinning the behaviour here means the resolver does not depend on which
        interpreter it happens to run under.
        """

        def redirect_request(self, req, fp, code, msg, headers, newurl):
            new = super().redirect_request(req, fp, code, msg, headers, newurl)
            if new is not None and req.get_method() == "HEAD":
                new.get_method = lambda: "HEAD"  # noqa: E731
            return new

    opener = urllib.request.build_opener(_KeepMethodOnRedirect)
    request = urllib.request.Request(uri, method="HEAD")
    with opener.open(request, timeout=15) as response:
        etag = response.headers.get("ETag")
        if etag:
            return "etag", etag.strip()
        modified = response.headers.get("Last-Modified")
        if modified:
            return "last-modified", modified.strip()
    raise ResolverError(
        f"{uri} returned neither ETag nor Last-Modified; there is no version "
        f"token to track without fetching the body, which this module does not do"
    )


# Only resolvers that need no credentials and work on any machine ship here.
# Anything requiring an API token, a tenant, or an admin's approval belongs in
# a connector, which registers itself via `register_resolver`.
# --------------------------------------------------------------------------- #
# admission — what a participant may point at
# --------------------------------------------------------------------------- #

#: Schemes a participant may name. Declared by the operator, same env shape as
#: `ASOP_VERIFIERS`, and deliberately NOT defaulting to everything registered.
#:
#: Registering a resolver says "this plane knows how to read that"; it does not
#: say "any participant may ask it to". Those were the same decision until an
#: assessment pointed out what it buys an untrusted caller: `file:` returns a
#: digest for any path and a distinct reason when the path is absent, which is a
#: file-existence and integrity oracle over the whole host — the registry key
#: file and `.env` included. `git:` runs `git rev-parse` against any directory,
#: enumerating private repositories. `http(s)` HEADs any address the caller
#: names, and reachable, refused and timed-out are distinguishable, which is a
#: port scanner with an authenticated front door.
#:
#: And it is not one request: `check_all` re-resolves every live snapshot on a
#: cadence for the TTL — 90 days by default — so one accepted write keeps firing.
SCHEMES_ENV_VAR = "ASOP_SNAPSHOT_SCHEMES"
FILE_ROOTS_ENV_VAR = "ASOP_SNAPSHOT_FILE_ROOTS"

#: Hosts an operator has deliberately named as reachable, exempt from the
#: internal-address check. An operator whose artifact server sits on a private
#: network needs this; a PARTICIPANT naming the same address does not get it,
#: which is the whole distinction — the check is about who chose the address,
#: and an allowlist is how the operator does the choosing.
HTTP_HOSTS_ENV_VAR = "ASOP_SNAPSHOT_HTTP_HOSTS"

#: Fails CLOSED, and the default is the safe half rather than none. `https` and
#: `http` still need the network check below, but they cannot name a path on
#: this host. `file:` and `git:` read the plane's own filesystem and are off
#: unless an operator turns them on AND says where.
DEFAULT_SCHEMES = ("https", "http")

#: The schemes this module ships, and the only ones admission gates.
#:
#: A scheme a CONNECTOR registered is admitted by the act of installing the
#: connector — that is an operator decision already, and gating it again would
#: mean an installed connector silently does nothing until a second, separate
#: declaration is made. These four are different: they are always present, and
#: they read the plane's own filesystem and network. Nobody chose them.
BUILTIN_SCHEMES = frozenset({"file", "git", "http", "https"})

_PRIVATE_V4 = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("0.0.0.0/8"),
)


def _declared(name: str, default: tuple[str, ...]) -> frozenset[str]:
    raw = os.environ.get(name)
    if raw is None:
        return frozenset(default)
    return frozenset(p.strip() for p in raw.split(",") if p.strip())


def _host_is_internal(host: str) -> bool:
    """True if this name resolves anywhere a participant should not reach.

    Resolved rather than pattern-matched: `localhost`, `127.1`, `0x7f.1`,
    `2130706433` and a hostname whose A record is 169.254.169.254 are all the
    same request wearing different spellings, and only resolution sees that.
    Unresolvable counts as internal — a name we cannot check is not a name we
    can clear.
    """
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return True
    for info in infos:
        addr = info[4][0].split("%")[0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            return True
        if ip.is_loopback or ip.is_link_local or ip.is_private or ip.is_reserved:
            return True
        if ip.version == 4 and any(ip in net for net in _PRIVATE_V4):
            return True
    return False


def admission_reason(uri: str) -> Optional[str]:
    """Why this URI may not be resolved, or None if it may.

    A REASON, not an exception, because this module already models "recorded but
    unresolvable" as a first-class outcome — so a refused scheme lands in a path
    the docs already describe, and the snapshot is still recorded. The caller
    learns the pointer was kept and not followed, which is the truth.
    """
    parsed = urlparse(uri)
    scheme = (parsed.scheme or "").lower()
    if scheme not in BUILTIN_SCHEMES:
        return None  # a connector's scheme; installing it was the decision
    allowed = _declared(SCHEMES_ENV_VAR, DEFAULT_SCHEMES)
    if scheme not in allowed:
        return (
            f"scheme {scheme!r} is not in the operator's declared set "
            f"({', '.join(sorted(allowed)) or 'none'}). Recorded, not followed — "
            f"declare {SCHEMES_ENV_VAR} to allow it."
        )
    if scheme in ("file", "git"):
        roots = _declared(FILE_ROOTS_ENV_VAR, ())
        if not roots:
            return (
                f"{scheme}: reads this host's filesystem and no roots are "
                f"declared. Recorded, not followed — set {FILE_ROOTS_ENV_VAR} to "
                f"the directories a participant may point at."
            )
        target = Path(parsed.path or parsed.netloc)
        try:
            resolved = target.resolve()
        except OSError:
            return f"{target} could not be resolved to a real path"
        if not any(
            resolved == Path(r).resolve() or Path(r).resolve() in resolved.parents
            for r in roots
        ):
            return (
                f"{resolved} is outside every declared root. Recorded, not "
                f"followed — symlinks are resolved before this comparison."
            )
        return None
    if scheme in ("http", "https"):
        host = parsed.hostname
        if not host:
            return f"{uri!r} names no host"
        if host in _declared(HTTP_HOSTS_ENV_VAR, ()):
            return None
        if _host_is_internal(host):
            return (
                f"{host} resolves to a loopback, link-local, private or reserved "
                f"address. Recorded, not followed — the plane does not reach "
                f"into its own network on a participant's behalf. An operator "
                f"who means to allow it names it in {HTTP_HOSTS_ENV_VAR}."
            )
    return None


RESOLVERS: dict[str, Callable[[str], tuple[str, str]]] = {
    "git": resolve_git,
    "file": resolve_file,
    "https": resolve_https,
    # Plain http too. An internal wiki or artifact server on a private network
    # frequently is not TLS-terminated, and having no resolver for it means
    # those pointers are silently recorded as unresolvable — which looks like a
    # missing connector rather than a scheme nobody registered.
    "http": resolve_https,
}


def register_resolver(scheme: str, resolver: Callable[[str], tuple[str, str]]) -> None:
    """Let a connector teach the registry how to read one URI scheme.

    A resolver returns `(hash_kind, version_token)` and must obtain that token
    WITHOUT fetching the artifact body — an eTag, a revision number, a commit
    id. A connector that downloads a document to hash it has broken the one
    invariant this module exists to hold, and no test here can catch that on
    the connector's behalf.

    Re-registering a scheme replaces it, deliberately: a deployment may have a
    better resolver for a scheme than the one that shipped.
    """
    RESOLVERS[scheme.lower()] = resolver


def resolve(uri: str) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """(hash_kind, content_hash, unresolved_reason). Never raises for an unknown scheme.

    An unregistered scheme returns a REASON rather than an exception, because
    the snapshot is still worth recording — it simply cannot report divergence
    until a connector for that scheme is installed, and the caller is told
    exactly that in the receipt.
    """
    scheme = (urlparse(uri).scheme or "").lower()
    if not scheme:
        raise Refusal(
            code="bad_uri",
            message=f"artifactUri {uri!r} has no scheme",
            remediation=(
                "Use a scheme-qualified URI — 'git:', 'file:' and 'https:' are built in, "
                "and a connector may register others. An unregistered scheme is still "
                "recorded, but cannot report divergence until its connector is installed."
            ),
        )
    refused = admission_reason(uri)
    if refused is not None:
        return None, None, refused
    resolver = RESOLVERS.get(scheme)
    if resolver is None:
        return None, None, (
            f"no resolver registered for scheme {scheme!r} — install a connector "
            f"that provides one, and this pointer starts reporting divergence "
            f"at the next check with no re-snapshot needed"
        )
    try:
        kind, value = resolver(uri)
    except ResolverError as exc:
        return None, None, str(exc)
    return kind, value, None


def take(
    conn: sqlite3.Connection,
    *,
    actor: str,
    artifact_uri: str,
    purpose: str,
    ttl_days: int = DEFAULT_TTL_DAYS,
    now: Optional[datetime] = None,
    agent_label: Optional[str] = None,
) -> dict:
    """Record a pointer plus its current version token. Returns the receipt."""
    at = now or datetime.now(timezone.utc)
    if not (purpose or "").strip():
        raise Refusal(
            code="purpose_required",
            message="a snapshot must say what it is a baseline FOR",
            remediation=(
                "Include 'purpose' — e.g. 'prototype baseline for the redesign'. The "
                "divergence notice quotes it back, and a notice that cannot say why you "
                "snapshotted something is one nobody acts on."
            ),
        )

    hash_kind, content_hash, unresolved = resolve(artifact_uri)
    uid = f"snap_{uuid.uuid4().hex[:12]}"
    expires = at + timedelta(days=ttl_days)

    with conn:
        conn.execute(
            "INSERT INTO snapshots(uid, actor, artifact_uri, purpose, hash_kind, "
            "content_hash, taken_at, expires_at, last_seen_hash, last_checked_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                uid,
                actor,
                artifact_uri,
                purpose.strip(),
                hash_kind or "",
                content_hash or "",
                _iso(at),
                _iso(expires),
                content_hash or "",
                _iso(at) if content_hash else None,
            ),
        )

    events.append(
        conn,
        kind="SnapshotTaken",
        actor=actor,
        agent_label=agent_label,
        occurred_at=_iso(at),
        payload={
            "snapId": uid,
            "artifactUri": artifact_uri,
            "purpose": purpose.strip(),
            "hashKind": hash_kind,
            "contentHash": content_hash,
            "resolution": "resolved" if content_hash else "unresolvable",
        },
    )

    receipt = {
        "snapId": uid,
        "state": "accepted",
        "artifactUri": artifact_uri,
        "hashKind": hash_kind,
        "contentHash": content_hash,
        "expiresAt": _iso(expires),
        # Never silent: the caller is told in the receipt that this pointer
        # cannot fire, and why.
        "freshness": {
            "externalResolution": "resolved" if content_hash else "unresolvable",
            "reason": unresolved,
        },
    }
    if unresolved:
        receipt["warning"] = (
            f"Recorded, but this pointer cannot report divergence yet: {unresolved}. "
            f"It will begin reporting the first time a check can read its version token."
        )
    return receipt


def check_all(
    conn: sqlite3.Connection,
    now: Optional[datetime] = None,
) -> list[dict]:
    """Re-read every live snapshot's version token. Accumulates; delivers nothing.

    Delivery is `divergence.digest`, at the cadence boundary. This split is
    the product, not an implementation detail: "Real-time divergence pings
    would be exactly the thing she is already drowning in" (docs/architecture.md (3)).
    """
    at = now or datetime.now(timezone.utc)
    rows = conn.execute(
        "SELECT * FROM snapshots WHERE expires_at > ? AND delivered_at IS NULL",
        (_iso(at),),
    ).fetchall()

    observed: list[dict] = []
    for row in rows:
        try:
            _, current, unresolved = resolve(row["artifact_uri"])
        except Refusal:
            # The artifact went away. That is itself a divergence signal, but
            # it is not a hash change; recorded as unresolvable so the digest
            # reports it in the honest line rather than as a false "changed".
            current, unresolved = None, "artifact no longer resolvable"
        with conn:
            conn.execute(
                "UPDATE snapshots SET last_seen_hash = ?, last_checked_at = ? WHERE uid = ?",
                (current or "", _iso(at), row["uid"]),
            )
        if not current or not row["content_hash"]:
            continue
        if current != row["content_hash"]:
            if row["diverged_at"] is None:
                with conn:
                    conn.execute(
                        "UPDATE snapshots SET diverged_at = ? WHERE uid = ?",
                        (_iso(at), row["uid"]),
                    )
            observed.append(
                {
                    "snapId": row["uid"],
                    "actor": row["actor"],
                    "artifactUri": row["artifact_uri"],
                    "purpose": row["purpose"],
                    "snapshotHash": row["content_hash"],
                    "currentHash": current,
                }
            )
    return observed
