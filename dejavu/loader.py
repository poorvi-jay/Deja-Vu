"""Write services and incidents from data/incidents.json into the graph.

This lives in the package rather than in scripts/ because two callers need it:
the CLI (`scripts/load_incidents.py`) and the API, which seeds itself on
startup when it finds an empty graph. A free-tier FalkorDB instance can be
recycled between demos, and a service that heals itself beats one that greets
a visitor with zero results.

Every write is a MERGE, so loading is idempotent: a second run creates nothing
and the graph never drifts.
"""

import json
from pathlib import Path

from dejavu.normalize import normalize

ROOT = Path(__file__).resolve().parent.parent
DATA_FILE = ROOT / "data" / "incidents.json"


# The values arrive as $parameters, never pasted into the query string. Two
# reasons: an apostrophe in a summary ("didn't") would otherwise break the
# Cypher syntax, and the database can reuse one query plan for all 27 loads.

SERVICE_QUERY = """
MERGE (svc:Service {name: $name})
  ON CREATE SET svc.team = $team, svc.language = $language
MERGE (team:Team {name: $team})
MERGE (svc)-[:OWNED_BY]->(team)
WITH svc
UNWIND $dependencies AS d
MERGE (dep:Dependency {name: d.name, version: d.version})
MERGE (svc)-[:DEPENDS_ON]->(dep)
"""
# `team` is keyed on its name, which is all a team is here — there is no team
# id, and two teams called "payments" would be one team. The property
# svc.team stays alongside the node: both are written from the same field of
# the same file, so they cannot drift, and the existing API responses keep
# working. The node is what you traverse; the property is a convenience.

ENGINEER_QUERY = """
MERGE (eng:Engineer {handle: $handle})
MERGE (team:Team {name: $team})
MERGE (eng)-[:MEMBER_OF]->(team)
"""
# Keyed on handle. A person's team can change; their handle is who they are.
# Putting team in the braces would create a second Engineer node the day
# someone moves team, and then "who has fixed this before" would answer twice
# for one person.
# UNWIND turns a list into one row per item, so the two MERGEs after it run
# once per dependency. Nothing follows it, which matters: UNWIND of an empty
# list produces zero rows, and any clause after it would silently not run.

INCIDENT_QUERY = """
MATCH (svc:Service {name: $service})
MERGE (inc:Incident {id: $id})
  ON CREATE SET
    inc.title       = $title,
    inc.occurred_at = $occurred_at,
    inc.severity    = $severity,
    inc.summary     = $summary
MERGE (sig:ErrorSignature {normalized: $error_signature})
MERGE (inc)-[:AFFECTED]->(svc)
MERGE (inc)-[:HAS_SIGNATURE]->(sig)
MERGE (res:Resolution {id: $resolution_id})
  ON CREATE SET
    res.action              = $resolution.action,
    res.fixed_by            = $resolution.fixed_by,
    res.time_to_resolve_min = $resolution.time_to_resolve_min
MERGE (inc)-[:RESOLVED_BY]->(res)
MERGE (eng:Engineer {handle: $resolution.fixed_by})
MERGE (res)-[:FIXED_BY]->(eng)
WITH inc
UNWIND $tags AS tag_name
MERGE (t:Tag {name: tag_name})
MERGE (inc)-[:TAGGED]->(t)
"""
# The tags go last for the same UNWIND reason as above: an incident with no
# tags would otherwise skip everything after the UNWIND.


class DataError(ValueError):
    """The data file is wrong in a way that would load silently."""


def read_data(path: Path = None) -> dict:
    return json.loads((path or DATA_FILE).read_text(encoding="utf-8"))


def validate(data: dict) -> None:
    """Catch data mistakes before touching the database.

    The important one: INCIDENT_QUERY starts with MATCH on the service. If an
    incident names a service that doesn't exist, MATCH finds nothing, and the
    whole query does nothing — no error, the incident is just silently missing.
    Checking here turns that silent failure into a loud one.
    """
    service_names = {s["name"] for s in data["services"]}
    engineers = {e["handle"] for e in data.get("engineers", [])}
    errors = []
    seen_ids = set()

    for inc in data["incidents"]:
        iid = inc.get("id", "<no id>")
        if inc["service"] not in service_names:
            errors.append(f"{iid}: unknown service '{inc['service']}'")
        if iid in seen_ids:
            errors.append(f"{iid}: duplicate incident id")
        seen_ids.add(iid)
        if not inc.get("tags"):
            errors.append(f"{iid}: has no tags")
        # A fixer with no engineers entry would still get an :Engineer node
        # from INCIDENT_QUERY, but with no team — so it would be invisible to
        # every routing query. Silent, and exactly the kind of hole that only
        # shows up when an advisory routes to nobody.
        if inc["resolution"]["fixed_by"] not in engineers:
            errors.append(
                f"{iid}: fixed_by '{inc['resolution']['fixed_by']}' has no engineers entry"
            )

    service_teams = {s["team"] for s in data["services"]}
    for eng in data.get("engineers", []):
        if eng["team"] not in service_teams:
            errors.append(
                f"engineer {eng['handle']}: team '{eng['team']}' owns no service"
            )

    if errors:
        raise DataError("Data problems in incidents.json:\n  " + "\n  ".join(errors))


def load_all(graph, data: dict = None) -> dict:
    """Load everything and report what was actually created."""
    data = data if data is not None else read_data()
    validate(data)
    totals = {"nodes": 0, "relationships": 0}

    # Services first: incidents MATCH on them, so they must already exist.
    for svc in data["services"]:
        result = graph.query(SERVICE_QUERY, params=svc)
        totals["nodes"] += result.nodes_created
        totals["relationships"] += result.relationships_created

    # Engineers before incidents, so MEMBER_OF is in place before anything
    # routes through it. INCIDENT_QUERY also MERGEs the engineer by handle;
    # because the key is the same, that finds this node rather than making a
    # second, teamless one.
    for eng in data.get("engineers", []):
        result = graph.query(ENGINEER_QUERY, params=eng)
        totals["nodes"] += result.nodes_created
        totals["relationships"] += result.relationships_created

    for inc in data["incidents"]:
        params = dict(
            inc,
            resolution_id=f"{inc['id']}-res",
            # Same function retrieval will use on incoming alerts, so the two
            # sides of a signature lookup can never be normalised differently.
            error_signature=normalize(inc["error_signature"]),
        )
        result = graph.query(INCIDENT_QUERY, params=params)
        totals["nodes"] += result.nodes_created
        totals["relationships"] += result.relationships_created

    return {
        "services": len(data["services"]),
        "engineers": len(data.get("engineers", [])),
        "incidents": len(data["incidents"]),
        "nodes_created": int(totals["nodes"]),
        "relationships_created": int(totals["relationships"]),
    }
