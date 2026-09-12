"""What a participant may point the plane at.

Found by an adversarial review. Registering a resolver says "this plane knows
how to read that"; it did not say "any participant may ask it to", and those
were the same decision. What that bought an untrusted caller, measured:

  * `file:` returns a digest for any path and a DISTINCT reason when the path is
    absent — a file-existence and integrity oracle over the whole host,
    including the registry key file and `.env`;
  * `git:` runs `git rev-parse` against any directory, enumerating private
    repositories and their HEADs;
  * `http(s)` HEADs any address the caller names, and reachable, refused and
    timed-out are distinguishable — a port scanner with an authenticated front
    door.

And it is not one request: `check_all` re-resolves every live snapshot on a
cadence for the TTL, 90 days by default, so one accepted write keeps firing.

These tests own the CLOSED cases and build their own environment; the suite's
autouse fixture declares a permissive set for tests that are about recording
rather than admission.
"""

from __future__ import annotations

import pytest

from agentco import snapshots


@pytest.fixture(autouse=True)
def _closed(monkeypatch):
    """Nothing declared — the shipped default."""
    for var in (
        snapshots.SCHEMES_ENV_VAR,
        snapshots.FILE_ROOTS_ENV_VAR,
        snapshots.HTTP_HOSTS_ENV_VAR,
    ):
        monkeypatch.delenv(var, raising=False)


def test_file_reads_are_off_by_default():
    """The oracle. `file:/etc/hosts` returned a sha256 before this existed.

    Two gates stand in front of it and this is the first: `file` is not in the
    shipped default scheme set, because a scheme that reads the plane's own
    disk should be something an operator turned on rather than something they
    would have to notice and turn off.
    """
    _, _, reason = snapshots.resolve("file:/etc/hosts")
    assert reason and "not in the operator's declared set" in reason


def test_git_enumeration_is_off_by_the_same_rule():
    _, _, reason = snapshots.resolve("git:/tmp#HEAD")
    assert reason and "not in the operator's declared set" in reason


def test_declaring_the_scheme_is_not_declaring_where(monkeypatch):
    """The second gate. Turning `file:` on does not say WHICH files — an
    operator who wants artifact snapshots of one directory should not thereby
    hand over the whole filesystem."""
    monkeypatch.setenv(snapshots.SCHEMES_ENV_VAR, "file,git")
    _, _, reason = snapshots.resolve("file:/etc/hosts")
    assert reason and "no roots are declared" in reason


@pytest.mark.parametrize(
    "host",
    ["127.0.0.1", "localhost", "169.254.169.254", "10.0.0.1", "192.168.1.1"],
)
def test_the_plane_does_not_reach_into_its_own_network_for_a_participant(host):
    """Resolved, not pattern-matched: `localhost`, `127.1` and a hostname whose
    A record is link-local are the same request in different spellings."""
    _, _, reason = snapshots.resolve(f"http://{host}/latest/meta-data/")
    assert reason and "not followed" in reason


def test_a_declared_root_is_readable_and_its_parent_is_not(monkeypatch, tmp_path):
    """The rail must not become a wall."""
    (tmp_path / "artifact.txt").write_text("hello")
    monkeypatch.setenv(snapshots.SCHEMES_ENV_VAR, "file")
    monkeypatch.setenv(snapshots.FILE_ROOTS_ENV_VAR, str(tmp_path))

    kind, value, reason = snapshots.resolve(f"file:{tmp_path}/artifact.txt")
    assert (kind, reason) == ("sha256", None)
    assert value

    _, _, escaped = snapshots.resolve("file:/etc/hosts")
    assert escaped and "outside every declared root" in escaped


def test_a_symlink_cannot_walk_out_of_a_declared_root(monkeypatch, tmp_path):
    """The check resolves before comparing, so a link inside a declared root
    does not become a door out of it."""
    monkeypatch.setenv(snapshots.SCHEMES_ENV_VAR, "file")
    monkeypatch.setenv(snapshots.FILE_ROOTS_ENV_VAR, str(tmp_path))
    (tmp_path / "escape").symlink_to("/etc/hosts")

    _, _, reason = snapshots.resolve(f"file:{tmp_path}/escape")
    assert reason and "outside every declared root" in reason


def test_an_operator_may_name_an_internal_host_deliberately(monkeypatch):
    """The distinction the whole check draws: who chose the address.

    An operator whose artifact server is on a private network needs to allow it.
    A participant supplying the same address does not get to.
    """
    monkeypatch.setenv(snapshots.HTTP_HOSTS_ENV_VAR, "127.0.0.1")
    assert snapshots.admission_reason("http://127.0.0.1:9/x") is None
    assert snapshots.admission_reason("http://10.0.0.5/x") is not None


def test_a_connectors_scheme_is_admitted_by_installing_the_connector():
    """Admission gates the four built-ins, which are always present and read
    this host. A connector's scheme was an operator decision already, and
    gating it twice would make an installed connector silently do nothing."""
    assert snapshots.admission_reason("s3://bucket/key") is None
    assert "file" in snapshots.BUILTIN_SCHEMES


def test_a_refusal_is_recorded_not_raised():
    """This module already models 'recorded but unresolvable' as a first-class
    outcome, so a refused scheme lands in a path the docs describe — the
    snapshot is kept, the pointer is simply not followed."""
    kind, value, reason = snapshots.resolve("file:/etc/hosts")
    assert (kind, value) == (None, None)
    assert reason


def test_a_revision_shaped_like_an_option_cannot_steer_git(monkeypatch, tmp_path):
    """`rev` is the URI fragment and reaches argv unescaped.

    No shell, so not RCE — but git reads a leading `-` as an option, so a caller
    could steer the command rather than name a revision. `--end-of-options`
    says everything after it is an operand.
    """
    import subprocess

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "f").write_text("x")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "-c", "user.email=a@b.c", "-c", "user.name=t",
         "commit", "-qm", "x"], check=True,
    )
    monkeypatch.setenv(snapshots.SCHEMES_ENV_VAR, "git")
    monkeypatch.setenv(snapshots.FILE_ROOTS_ENV_VAR, str(tmp_path))

    kind, value, reason = snapshots.resolve(f"git:{tmp_path}#HEAD")
    assert (kind, reason) == ("git-sha", None)
    # 40 hex characters and nothing else. The separator echoes on its own line,
    # so taking the whole stdout returned "--end-of-options\n<sha>" — a version
    # token every later comparison would have read as a changed artifact.
    assert len(value) == 40 and all(c in "0123456789abcdef" for c in value)

    _, _, refused = snapshots.resolve(f"git:{tmp_path}#--version")
    assert refused
