"""Tests for retrieval.

The unit tests need no database. The integration tests do, and they skip with
a clear message if one is not reachable — but they are meant to RUN. A test
that always skips proves nothing.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from dejavu.retrieve import Alert, Match, at_risk_services, find_similar  # noqa: E402


# --------------------------------------------------------------------------
# Unit tests
# --------------------------------------------------------------------------


def test_alert_normalises_its_error():
    alert = Alert(
        service="refund-service",
        error="HikariPool-1 - Connection is not available, request timed out after 30000ms.",
    )
    assert alert.signature == (
        "HikariPool-1 - Connection is not available, request timed out after Nms"
    )


def make_match(**overrides) -> Match:
    defaults = dict(
        id="INC-001",
        title="t",
        severity="SEV2",
        occurred_at="2025-08-14T19:05:00Z",
        service="checkout-api",
        score=10,
        signature_match=False,
        shared_tags=[],
        shared_dependencies=[],
        same_service=False,
        resolution=None,
        fixed_by=None,
    )
    defaults.update(overrides)
    return Match(**defaults)


def test_why_reports_signature_match():
    assert "identical error signature" in make_match(signature_match=True).why()


def test_why_lists_shared_tags_sorted():
    why = make_match(shared_tags=["timeout", "connection-pool"]).why()
    assert "shared tags: connection-pool, timeout" in why


def test_why_never_cites_a_shared_dependency():
    """The query counts any library two services have in common, so naming one
    as a reason would imply a causal link the graph cannot vouch for."""
    match = make_match(signature_match=True, shared_dependencies=["HikariCP"])
    assert "HikariCP" not in match.why()
    assert match.also_shares() == "HikariCP"


def test_also_shares_is_empty_for_the_same_service():
    match = make_match(same_service=True, shared_dependencies=["HikariCP"])
    assert match.also_shares() == ""


def test_why_with_no_evidence():
    assert make_match().why() == "no evidence"


# --------------------------------------------------------------------------
# Integration tests — these talk to the real graph
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def graph():
    try:
        from run_cypher import connect, load_env

        load_env()
        g = connect()
        g.ro_query("RETURN 1")
        return g
    except SystemExit as exc:  # load_env exits when .env is missing
        pytest.skip(f"no database configured: {exc}")
    except Exception as exc:
        pytest.skip(f"database not reachable: {exc}")


@pytest.fixture
def pool_alert():
    return Alert(
        service="refund-service",
        error="HikariPool-1 - Connection is not available, request timed out after 30000ms.",
        tags=["connection-pool", "timeout", "database"],
    )


def test_graph_is_loaded(graph):
    result = graph.ro_query("MATCH (i:Incident) RETURN count(i) AS n")
    assert result.result_set[0][0] == 27


def test_signature_match_ranks_above_tags_only(graph, pool_alert):
    """INC-001 raises the identical error. INC-019 is the same root cause with
    different wording, reachable only through tags. Both must be found, and
    the signature match must rank higher."""
    matches = {m.id: m for m in find_similar(graph, pool_alert, limit=10)}
    assert "INC-001" in matches
    assert "INC-019" in matches
    assert matches["INC-001"].signature_match is True
    assert matches["INC-019"].signature_match is False
    assert matches["INC-001"].score > matches["INC-019"].score


def test_matches_are_ordered_by_score(graph, pool_alert):
    scores = [m.score for m in find_similar(graph, pool_alert, limit=10)]
    assert scores == sorted(scores, reverse=True)


def test_every_match_can_explain_itself(graph, pool_alert):
    for m in find_similar(graph, pool_alert, limit=10):
        assert m.why() != "no evidence"


def test_limit_is_respected(graph, pool_alert):
    assert len(find_similar(graph, pool_alert, limit=3)) == 3


def test_single_generic_tag_does_not_qualify(graph, pool_alert):
    """INC-012 is a Redis hot-key incident. It overlaps the alert on `timeout`
    alone, which a fifth of all incidents carry, and it must not be returned
    as similar to a connection-pool failure."""
    ids = {m.id for m in find_similar(graph, pool_alert, limit=20)}
    assert "INC-012" not in ids
    assert "INC-019" in ids  # three shared tags, still qualifies


def test_everything_returned_has_real_evidence(graph, pool_alert):
    for m in find_similar(graph, pool_alert, limit=20):
        assert m.signature_match or m.same_service or len(m.shared_tags) >= 2


def test_tags_only_alert_still_finds_incidents(graph):
    """An alert whose error text matches nothing stored. Retrieval must fall
    back to tags rather than returning nothing."""
    alert = Alert(
        service="api-gateway",
        error="tls: failed to verify certificate: something we have never seen",
        tags=["tls", "certificate"],
    )
    matches = find_similar(graph, alert, limit=5)
    assert matches
    assert all(m.signature_match is False for m in matches)
    assert {"INC-002", "INC-015"} <= {m.id for m in matches}


def test_unknown_error_with_no_tags_finds_nothing(graph):
    """No signature, no tags, and a service with no history: the honest answer
    is an empty list, not a low-quality guess."""
    alert = Alert(service="refund-service", error="totally novel failure mode", tags=[])
    assert find_similar(graph, alert, limit=5) == []


def test_at_risk_finds_peers_through_shared_dependency(graph, pool_alert):
    peers = {p["service"]: p for p in at_risk_services(graph, pool_alert)}
    # refund-service shares HikariCP with exactly these three.
    assert set(peers) == {"checkout-api", "order-service", "inventory-service"}
    assert all("HikariCP" in p["via"] for p in peers.values())
    assert all(int(p["past_incidents"]) > 0 for p in peers.values())
