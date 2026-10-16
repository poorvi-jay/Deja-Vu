"""Who to tell, and who already knows.

Retrieval answers "have we seen this before". This answers the questions that
follow: who owns the exposed service, and who has actually fixed this kind of
failure before — even when they sit in another team.

Both are traversals that were impossible before `team` and `fixed_by` stopped
being properties. You can walk to a node; you cannot walk to a property.
"""

from dataclasses import dataclass
from typing import List, Optional

from dejavu.retrieve import _rows

# Tag -> incidents carrying it -> their resolutions -> the engineer who wrote
# each -> that engineer's team. Five hops, and the count is what ranks them:
# someone who has fixed this three times is a better person to ask than
# someone who has fixed it once.
EXPERTS_QUERY = """
MATCH (:Tag {name: $family})<-[:TAGGED]-(i:Incident)
      -[:RESOLVED_BY]->(:Resolution)-[:FIXED_BY]->(eng:Engineer)-[:MEMBER_OF]->(t:Team)
RETURN eng.handle AS engineer, t.name AS team,
       count(DISTINCT i) AS fixes, collect(DISTINCT i.id) AS incidents
ORDER BY fixes DESC, engineer
"""

OWNER_QUERY = """
MATCH (:Service {name: $service})-[:OWNED_BY]->(t:Team)
OPTIONAL MATCH (eng:Engineer)-[:MEMBER_OF]->(t)
RETURN t.name AS team, collect(DISTINCT eng.handle) AS members
"""


@dataclass
class Expert:
    engineer: str
    team: str
    fixes: int
    incidents: List[str]


@dataclass
class Owner:
    team: str
    members: List[str]


def experts_for_family(graph, family: str) -> List[Expert]:
    """Engineers who have fixed this failure family, most experienced first."""
    return [
        Expert(
            engineer=r["engineer"],
            team=r["team"],
            fixes=int(r["fixes"]),
            incidents=sorted(r["incidents"] or []),
        )
        for r in _rows(graph.ro_query(EXPERTS_QUERY, params={"family": family}))
    ]


def owner_of(graph, service: str) -> Optional[Owner]:
    """The team accountable for a service, or None if the service is unknown."""
    rows = _rows(graph.ro_query(OWNER_QUERY, params={"service": service}))
    if not rows:
        return None
    return Owner(team=rows[0]["team"], members=sorted(rows[0]["members"] or []))


def route(graph, service: str, family: str) -> dict:
    """Who to tell about an exposure, and who to ask about it.

    Deliberately two answers, not one. The owning team is accountable and
    always exists; the expert may sit elsewhere, and routing only to an
    individual breaks the week they are on leave. Where an expert is also on
    the owning team they are the obvious first ask, so that case is marked.
    """
    owner = owner_of(graph, service)
    experts = experts_for_family(graph, family)
    in_team = [e for e in experts if owner and e.team == owner.team]
    return {
        "service": service,
        "family": family,
        "owner": owner,
        "experts": experts,
        "ask_first": in_team[0] if in_team else (experts[0] if experts else None),
        "expertise_is_outside_the_owning_team": bool(experts) and not in_team,
    }
