"""Advisories: dedupe, status rules, evidence.

These are the tests the feature lives or dies by. Deduplication is the whole
reason the identity key is what it is, and the reopen-vs-dismiss split is the
difference between a signal and something people mute.

Every test cleans up after itself: these WRITE to the shared graph, and a
leftover advisory would make a later run pass for the wrong reason.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from dejavu.advisory import (  # noqa: E402
    DISMISSED,
    OPEN,
    RESOLVED,
    advisory_id,
    list_advisories,
    raise_advisories,
    set_status,
)
from dejavu.retrieve import Alert, find_similar  # noqa: E402

POOL_ALERT = Alert(
    service="refund-service",
    error="HikariPool-1 - Connection is not available, request timed out after 30000ms.",
    tags=["connection-pool", "timeout", "database"],
)


@pytest.fixture(scope="module")
def graph():
    try:
        from run_cypher import connect, load_env

        load_env()
        g = connect()
        g.ro_query("RETURN 1")
        return g
    except SystemExit as exc:
        pytest.skip(f"no database configured: {exc}")
    except Exception as exc:
        pytest.skip(f"database not reachable: {exc}")


@pytest.fixture
def clean(graph):
    """Remove every advisory before and after each test."""
    graph.query("MATCH (a:Advisory) DETACH DELETE a")
    yield
    graph.query("MATCH (a:Advisory) DETACH DELETE a")


def matches_for(graph, alert=POOL_ALERT):
    return find_similar(graph, alert, limit=5)


# --------------------------------------------------------------------------
# Identity
# --------------------------------------------------------------------------


def test_advisory_id_is_service_and_family():
    """Not the dependency: two implicated dependencies are one exposure with
    two reasons, not two exposures."""
    assert advisory_id("refund-service", "connection-pool") == (
        "refund-service:connection-pool"
    )


# --------------------------------------------------------------------------
# Raising
# --------------------------------------------------------------------------


def test_nothing_is_raised_when_nothing_matched(graph, clean):
    """No evidence, no advisory. An exposure claim needs something behind it."""
    alert = Alert(service="refund-service", error="totally novel failure", tags=[])
    assert raise_advisories(graph, alert, matches_for(graph, alert)) == []
    assert list_advisories(graph) == []


def test_peers_that_have_already_had_this_are_not_warned(graph, clean):
    """checkout-api, order-service and inventory-service have all hit pool
    exhaustion. Warning them about it is noise."""
    matches = matches_for(graph)
    raise_advisories(graph, POOL_ALERT, matches)

    warned = {a.service for a in list_advisories(graph)}
    assert "checkout-api" not in warned
    assert "order-service" not in warned
    assert "inventory-service" not in warned


def test_an_unaffected_peer_is_warned(graph, clean):
    """The alerting service is refund-service, so its peers are the three that
    have had pool incidents — all skipped. Raising from a service whose peer
    has a clean record produces an advisory for that peer."""
    alert = Alert(
        service="checkout-api",
        error="HikariPool-1 - Connection is not available, request timed out after 30000ms.",
        tags=["connection-pool", "timeout"],
    )
    matches = matches_for(graph, alert)
    raised = raise_advisories(graph, alert, matches)

    assert {a.service for a in raised} == {"refund-service", "billing-worker"}

    advisory = next(a for a in raised if a.service == "refund-service")
    assert advisory.status == OPEN
    # One advisory, two implicated dependencies: the pool library and the
    # primary every affected service talks to.
    assert advisory.via == ["HikariCP", "postgres-primary"]
    assert advisory.seen_count == 1


def test_an_advisory_carries_its_evidence_and_a_fix(graph, clean):
    alert = Alert(
        service="checkout-api",
        error="HikariPool-1 - Connection is not available, request timed out after 30000ms.",
        tags=["connection-pool", "timeout"],
    )
    raised = raise_advisories(graph, alert, matches_for(graph, alert))
    advisory = next(a for a in raised if a.service == "refund-service")

    assert advisory.because_of, "must say which incidents prompted it"
    assert all(i.startswith("INC-") for i in advisory.because_of)
    assert advisory.suggested_fix, "must carry the fix that worked elsewhere"
    assert advisory.routed_to == "payments", "refund-service is owned by payments"
    assert advisory.ask, "must name someone to ask"
    assert "HikariCP" in advisory.why()


# --------------------------------------------------------------------------
# Deduplication — the point of the identity key
# --------------------------------------------------------------------------


@pytest.fixture
def pool_alert_on_checkout():
    return Alert(
        service="checkout-api",
        error="HikariPool-1 - Connection is not available, request timed out after 30000ms.",
        tags=["connection-pool", "timeout"],
    )


def test_raising_twice_produces_one_advisory(graph, clean, pool_alert_on_checkout):
    alert = pool_alert_on_checkout
    matches = matches_for(graph, alert)

    raise_advisories(graph, alert, matches)
    first = list_advisories(graph)
    raise_advisories(graph, alert, matches)
    second = list_advisories(graph)

    assert len(first) == len(second) == 2
    assert {a.id for a in first} == {a.id for a in second}


def test_raising_twice_counts_the_recurrence(graph, clean, pool_alert_on_checkout):
    """One node, but the fact it happened again is recorded — that is what a
    timestamp check would have been for, and it comes free from ON MATCH."""
    alert = pool_alert_on_checkout
    matches = matches_for(graph, alert)

    raise_advisories(graph, alert, matches)
    raise_advisories(graph, alert, matches)
    raise_advisories(graph, alert, matches)

    assert all(a.seen_count == 3 for a in list_advisories(graph))


def test_family_is_part_of_the_identity():
    """Same service, same dependency, different family — different advisory.
    Asserted on the id rather than through the graph, because on this data no
    peer is exposed in two families at once (see the next test for why)."""
    pool = advisory_id("refund-service", "connection-pool")
    cache = advisory_id("refund-service", "cache")
    assert pool != cache


def test_a_dependency_unrelated_to_the_family_warns_nobody(graph, clean):
    """The conservative rule, and the bug it fixes.

    A cache alert on checkout-api used to warn order-service "you share
    HikariCP" — a connection-pool library that has nothing to do with caches.
    Cache incidents are spread across user-auth, inventory-service and
    checkout-api, which share no dependency at all, so no dependency is
    implicated and nobody is warned. Silence is the correct answer.
    """
    cache = Alert(
        service="checkout-api",
        error="RedisCommandTimeoutException: Command timed out after 5 s",
        tags=["cache", "redis"],
    )
    matches = matches_for(graph, cache)
    assert matches, "the alert should still find similar past incidents"
    assert raise_advisories(graph, cache, matches) == []
    assert list_advisories(graph) == []


# --------------------------------------------------------------------------
# Status rules
# --------------------------------------------------------------------------


def test_a_resolved_advisory_reopens_when_the_family_recurs(
    graph, clean, pool_alert_on_checkout
):
    """The ordering of SET items in RAISE_QUERY is load-bearing: reopened_at
    reads the OLD status, so it must be set before status is overwritten.
    This test is why that is not left to a comment."""
    alert = pool_alert_on_checkout
    matches = matches_for(graph, alert)
    raise_advisories(graph, alert, matches)
    aid = advisory_id("refund-service", "connection-pool")

    set_status(graph, aid, RESOLVED)
    assert next(a for a in list_advisories(graph) if a.id == aid).status == RESOLVED

    raise_advisories(graph, alert, matches)
    reopened = next(a for a in list_advisories(graph) if a.id == aid)
    assert reopened.status == OPEN
    assert reopened.reopened_at is not None
    assert reopened.seen_count == 2


def test_a_dismissed_advisory_stays_dismissed(graph, clean, pool_alert_on_checkout):
    """Someone decided this does not apply to them. Re-raising must not
    re-nag — that is how a tool gets muted — but the recurrence is still
    counted, so the decision can be revisited with evidence."""
    alert = pool_alert_on_checkout
    matches = matches_for(graph, alert)
    raise_advisories(graph, alert, matches)
    aid = advisory_id("refund-service", "connection-pool")

    set_status(graph, aid, DISMISSED)
    raise_advisories(graph, alert, matches)

    advisory = next(a for a in list_advisories(graph) if a.id == aid)
    assert advisory.status == DISMISSED
    assert advisory.reopened_at is None
    assert advisory.seen_count == 2


def test_filtering_by_status(graph, clean, pool_alert_on_checkout):
    alert = pool_alert_on_checkout
    raise_advisories(graph, alert, matches_for(graph, alert))
    everything = list_advisories(graph)
    assert len(list_advisories(graph, status=OPEN)) == len(everything)

    set_status(graph, everything[0].id, DISMISSED)
    assert len(list_advisories(graph, status=OPEN)) == len(everything) - 1
    assert len(list_advisories(graph, status=DISMISSED)) == 1


def test_filtering_by_team(graph, clean, pool_alert_on_checkout):
    alert = pool_alert_on_checkout
    raise_advisories(graph, alert, matches_for(graph, alert))

    # refund-service and billing-worker are both payments services.
    assert len(list_advisories(graph, team="payments")) == 2
    assert list_advisories(graph, team="platform") == []


def test_an_unknown_status_is_rejected(graph, clean):
    with pytest.raises(ValueError, match="unknown status"):
        set_status(graph, "whatever", "sort-of-fixed")


def test_setting_status_on_a_missing_advisory_returns_none(graph, clean):
    assert set_status(graph, "no-such-advisory", RESOLVED) is None
