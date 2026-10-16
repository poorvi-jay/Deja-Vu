"""Ownership and expertise routing.

These run against the real graph: the whole point is that `team` and
`fixed_by` are now reachable by traversal, and only a real graph proves it.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from dejavu.coordinate import experts_for_family, owner_of, route  # noqa: E402
from dejavu.loader import read_data  # noqa: E402


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


# --------------------------------------------------------------------------
# The promotions: team and fixed_by are now nodes
# --------------------------------------------------------------------------


def test_every_service_has_an_owning_team(graph):
    rows = graph.ro_query(
        "MATCH (s:Service) WHERE NOT (s)-[:OWNED_BY]->(:Team) RETURN collect(s.name)"
    ).result_set
    assert rows[0][0] == [], f"services with no owner: {rows[0][0]}"


def test_every_engineer_belongs_to_a_team(graph):
    """An engineer with no team is invisible to routing — silently, which is
    the failure mode validate() exists to prevent."""
    rows = graph.ro_query(
        "MATCH (e:Engineer) WHERE NOT (e)-[:MEMBER_OF]->(:Team) RETURN collect(e.handle)"
    ).result_set
    assert rows[0][0] == [], f"teamless engineers: {rows[0][0]}"


def test_every_resolution_names_an_engineer(graph):
    rows = graph.ro_query(
        "MATCH (r:Resolution) WHERE NOT (r)-[:FIXED_BY]->(:Engineer) RETURN collect(r.id)"
    ).result_set
    assert rows[0][0] == [], f"resolutions with no engineer: {rows[0][0]}"


def test_one_engineer_node_per_handle(graph):
    """INCIDENT_QUERY and ENGINEER_QUERY both MERGE engineers. Keyed the same
    way, so they must find each other rather than making duplicates."""
    expected = len(read_data()["engineers"])
    count = graph.ro_query("MATCH (e:Engineer) RETURN count(e)").result_set[0][0]
    assert int(count) == expected


def test_the_node_agrees_with_the_property_it_replaced(graph):
    """Both are written from the same field of the same file, so a mismatch
    means the loader has drifted."""
    rows = graph.ro_query(
        """
        MATCH (s:Service)-[:OWNED_BY]->(t:Team)
        WHERE s.team <> t.name
        RETURN collect(s.name)
        """
    ).result_set
    assert rows[0][0] == []


# --------------------------------------------------------------------------
# Expertise
# --------------------------------------------------------------------------


def test_experts_are_ranked_by_how_often_they_fixed_it(graph):
    experts = experts_for_family(graph, "kafka")
    assert experts
    assert [e.fixes for e in experts] == sorted((e.fixes for e in experts), reverse=True)
    assert experts[0].engineer == "marcus.o"  # three kafka fixes
    assert experts[0].fixes == 3


def test_an_expert_carries_the_incidents_that_back_the_claim(graph):
    """"marcus.o has fixed this three times" must be checkable."""
    top = experts_for_family(graph, "kafka")[0]
    assert len(top.incidents) == top.fixes
    assert {"INC-006", "INC-014", "INC-027"} == set(top.incidents)


def test_an_unknown_family_has_no_experts(graph):
    assert experts_for_family(graph, "no-such-family") == []


def test_owner_of_a_known_service(graph):
    owner = owner_of(graph, "refund-service")
    assert owner.team == "payments"
    assert "priya.n" in owner.members


def test_owner_of_an_unknown_service_is_none(graph):
    assert owner_of(graph, "no-such-service") is None


# --------------------------------------------------------------------------
# Routing — the two-answer rule
# --------------------------------------------------------------------------


def test_routing_prefers_an_expert_inside_the_owning_team(graph):
    """refund-service is payments, and priya.n is both on payments and has
    fixed connection-pool twice. She is the obvious first ask even though
    others have equal experience."""
    result = route(graph, "refund-service", "connection-pool")
    assert result["owner"].team == "payments"
    assert result["ask_first"].engineer == "priya.n"
    assert result["expertise_is_outside_the_owning_team"] is False


def test_routing_reaches_across_teams_when_it_has_to(graph):
    """The case a single-team model cannot express: search-api is owned by
    discovery, and every kafka expert sits elsewhere."""
    result = route(graph, "search-api", "kafka")
    assert result["owner"].team == "discovery"
    assert result["ask_first"].engineer == "marcus.o"
    assert result["ask_first"].team == "orders"
    assert result["expertise_is_outside_the_owning_team"] is True


def test_routing_still_names_an_owner_when_nobody_has_fixed_it(graph):
    """No expertise is not the same as nobody to tell."""
    result = route(graph, "refund-service", "no-such-family")
    assert result["owner"].team == "payments"
    assert result["experts"] == []
    assert result["ask_first"] is None
    assert result["expertise_is_outside_the_owning_team"] is False
