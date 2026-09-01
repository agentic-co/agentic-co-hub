"""The participation ladder's own revisit condition, made measurable.

The ADR commits the project to abandoning the ladder if L1 does not convert —
*"the outbox is not a floor, it is a terminus, and the config line was never the
obstacle."* That is a decision to delete a shipped feature, so the number behind
it has to be one that can actually distinguish the two outcomes.

The failure this file mostly exists to prevent is a metric that reads zero by
construction. Every test below is a case where the naive per-identity funnel
would report "nobody converted" while something entirely different is true.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agentco import db, metrics

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)  # a Tuesday


@pytest.fixture
def conn(tmp_path):
    return db.connect(tmp_path / "registry.sqlite3")


def call(conn, *, actor, via, label=None, verb="claim_scope", weeks_ago=1, status="accepted"):
    metrics.record_call(
        conn,
        verb=verb,
        actor=actor,
        status=status,
        latency_ms=1.0,
        at=NOW - timedelta(weeks=weeks_ago),
        agent_label=label,
        via=via,
    )


# --------------------------------------------------------------------------- #
# Nothing to measure is not a measurement
# --------------------------------------------------------------------------- #


def test_an_empty_registry_reports_none_and_not_zero(conn):
    """`None` and `0` are opposite findings. One says nobody has arrived at L1;
    the other says they arrived and stopped. A dashboard that renders them the
    same way will eventually get the wrong one believed — which is the rule the
    roadmap already states for usage metering: unreported is null, never 0."""
    report = metrics.l1_conversion(conn, now=NOW)
    assert report["conversionCount"] is None
    assert report["everSeenL1"] is False
    assert report["ladderFalsified"] is False
    assert "NOT zero conversion" in report["verdict"]


def test_direct_traffic_alone_still_reports_nothing_to_measure(conn):
    """A registry busy with configured harnesses and no outbox users has not
    failed the conversion test — it has never taken it."""
    for i in range(5):
        call(conn, actor=f"dana-{i}", via="direct")
    report = metrics.l1_conversion(conn, now=NOW)
    assert report["conversionCount"] is None
    assert len(report["l2Actors"]) == 5


# --------------------------------------------------------------------------- #
# The population signal, which needs no identity join
# --------------------------------------------------------------------------- #


def test_l1_and_l2_populations_are_counted_separately(conn):
    call(conn, actor="bigmac", via="outbox", label="cursor")
    call(conn, actor="bigmac", via="outbox", label="aider")
    call(conn, actor="dana", via="direct")
    report = metrics.l1_conversion(conn, now=NOW)
    assert report["l1Labels"] == ["aider", "cursor"]
    assert report["l2Actors"] == ["dana"]
    assert report["conversionCount"] == 0


def test_an_unlabelled_outbox_publisher_is_still_counted(conn):
    """The least-configured participants are exactly the population this metric
    watches, so a harness that reported no label must not fall out of the count
    — that would silently exclude the people the ladder is for."""
    call(conn, actor="bigmac", via="outbox", label=None)
    report = metrics.l1_conversion(conn, now=NOW)
    assert report["everSeenL1"] is True
    assert report["l1Labels"] == ["(unlabelled via bigmac)"]


def test_a_conversion_joins_on_the_authenticated_actor(conn):
    """The one join that is trustworthy. `agent_label` is self-reported and may
    be absent at L2 entirely; the actor is signed."""
    call(conn, actor="bigmac", via="outbox", label="cursor", weeks_ago=3)
    call(conn, actor="bigmac", via="direct", weeks_ago=1)
    report = metrics.l1_conversion(conn, now=NOW)
    assert report["conversions"] == ["bigmac"]
    assert report["conversionCount"] == 1
    assert report["ladderFalsified"] is False


def test_the_conversion_count_is_a_lower_bound_and_says_so(conn):
    """A colleague who publishes through the outbox on one machine and
    configures MCP on another is a real conversion this cannot see. The metric
    must under-report rather than guess, and must not present the number as
    complete."""
    call(conn, actor="bigmac", via="outbox", label="cursor")
    call(conn, actor="laptop", via="direct")
    report = metrics.l1_conversion(conn, now=NOW)
    assert report["conversionCount"] == 0
    assert "lower bound" in report["definitions"]["conversion"]


# --------------------------------------------------------------------------- #
# The falsification criterion, written before the data
# --------------------------------------------------------------------------- #


def test_three_stalled_harnesses_over_the_span_falsify_the_ladder(conn):
    for i, label in enumerate(("cursor", "aider", "codex")):
        call(conn, actor="bigmac", via="outbox", label=label, weeks_ago=i + 1)
    report = metrics.l1_conversion(conn, now=NOW)
    assert report["ladderFalsified"] is True
    assert "terminus" in report["verdict"]
    assert "alternative (a)" in report["verdict"]
    assert "lower bound" in report["verdict"], "it must not read as a settled verdict"


def test_two_stalled_harnesses_do_not_falsify_it(conn):
    """The threshold is three, chosen before any data existed: low enough to
    fire, high enough not to fire on one curious person and their colleague."""
    for i, label in enumerate(("cursor", "aider")):
        call(conn, actor="bigmac", via="outbox", label=label, weeks_ago=i + 1)
    assert metrics.l1_conversion(conn, now=NOW)["ladderFalsified"] is False


def test_one_conversion_clears_the_falsification_however_many_stalled(conn):
    for i, label in enumerate(("cursor", "aider", "codex", "zed")):
        call(conn, actor="bigmac", via="outbox", label=label, weeks_ago=i + 1)
    call(conn, actor="bigmac", via="direct", weeks_ago=1)
    assert metrics.l1_conversion(conn, now=NOW)["ladderFalsified"] is False


# --------------------------------------------------------------------------- #
# Windowing, matching the adoption gate
# --------------------------------------------------------------------------- #


def test_the_week_in_progress_is_excluded(conn):
    """Same rule as the adoption gate, for the same reason: a partial week
    always looks like a decline."""
    call(conn, actor="bigmac", via="outbox", label="cursor", weeks_ago=0)
    report = metrics.l1_conversion(conn, now=NOW)
    assert report["everSeenL1"] is False, "this week cannot be counted yet"
    assert report["l1Labels"] == []


def test_the_trailing_window_bounds_the_population_but_not_the_conversion(conn):
    """A conversion is a thing that happened, not a thing that is happening. An
    old arrival that later configured itself stays converted even once both
    events fall out of the four-week population window — otherwise the ladder
    would appear to un-succeed with the passage of time."""
    call(conn, actor="bigmac", via="outbox", label="cursor", weeks_ago=7)
    call(conn, actor="bigmac", via="direct", weeks_ago=6)
    report = metrics.l1_conversion(conn, now=NOW)
    assert report["l1Labels"] == [] and report["l2Actors"] == []
    assert report["conversionCount"] == 1


def test_refused_calls_do_not_count_as_arriving(conn):
    """A publisher is an identity with an ACCEPTED write. Counting refusals here
    would report an arrival for somebody whose every attempt bounced — which is
    the adoption failure, not the adoption."""
    call(conn, actor="bigmac", via="outbox", label="cursor", status="refused")
    assert metrics.l1_conversion(conn, now=NOW)["everSeenL1"] is False
