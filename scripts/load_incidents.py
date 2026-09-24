"""Load every service and incident from data/incidents.json into FalkorDB.

Usage:
    python scripts/load_incidents.py

This does exactly what cypher/01_first_incident.cypher does, but driven by
data instead of hand-written literals. Every write is a MERGE, so it is safe
to re-run: a second run should report 0 nodes and 0 relationships created.
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_FILE = ROOT / "data" / "incidents.json"

# Python only searches this script's own folder (scripts/) for imports, so the
# repo root has to be added before the dejavu package can be found.
sys.path.insert(0, str(ROOT))

from dejavu.normalize import normalize  # noqa: E402
# run_cypher.py sits next to this file, so Python can import it directly.
from run_cypher import connect, load_env  # noqa: E402


# The values arrive as $parameters, never pasted into the query string. Two
# reasons: an apostrophe in a summary ("didn't") would otherwise break the
# Cypher syntax, and the database can reuse one query plan for all 27 loads.

SERVICE_QUERY = """
MERGE (svc:Service {name: $name})
  ON CREATE SET svc.team = $team, svc.language = $language
WITH svc
UNWIND $dependencies AS d
MERGE (dep:Dependency {name: d.name, version: d.version})
MERGE (svc)-[:DEPENDS_ON]->(dep)
"""
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
WITH inc
UNWIND $tags AS tag_name
MERGE (t:Tag {name: tag_name})
MERGE (inc)-[:TAGGED]->(t)
"""
# The tags go last for the same UNWIND reason as above: an incident with no
# tags would otherwise skip everything after the UNWIND.


def validate(data):
    """Catch data mistakes before touching the database.

    The important one: INCIDENT_QUERY starts with MATCH on the service. If an
    incident names a service that doesn't exist, MATCH finds nothing, and the
    whole query does nothing — no error, the incident is just silently missing.
    Checking here turns that silent failure into a loud one.
    """
    service_names = {s["name"] for s in data["services"]}
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

    if errors:
        sys.exit("Data problems in incidents.json:\n  " + "\n  ".join(errors))


def main():
    load_env()
    data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    validate(data)

    graph = connect()
    totals = {"nodes": 0, "relationships": 0}

    # Services first: incidents MATCH on them, so they must already exist.
    for svc in data["services"]:
        result = graph.query(SERVICE_QUERY, params=svc)
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

    print(
        f"Loaded {len(data['services'])} services, {len(data['incidents'])} incidents.\n"
        f"nodes created: {int(totals['nodes'])}  "
        f"relationships created: {int(totals['relationships'])}"
    )


if __name__ == "__main__":
    main()
