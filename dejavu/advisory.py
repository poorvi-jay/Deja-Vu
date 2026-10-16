"""Advisories: telling the services that are exposed but not yet affected.

Retrieval says "this happened before, here is the fix". An advisory says "this
has not happened to you yet, and it probably will". It is raised for a peer
service that shares a dependency with the alerting one, carrying the evidence
and the fix that worked elsewhere.

The design decision that everything else follows from is **what makes two
advisories the same**: the exposed service, the shared dependency, and the
failure family. Not the incident that triggered it — `refund-service` would
then collect six advisories for six connection-pool incidents, and six
notifications about one exposure is noise people learn to mute.

Because identity is a key, deduplication is not a check anyone has to
remember to write. It is a consequence of MERGE finding the node that already
exists. Same lesson as the loader, applied to coordination.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional

from dejavu.coordinate import route
from dejavu.families import slug_of
from dejavu.retrieve import _rows

OPEN = "open"
ACKNOWLEDGED = "acknowledged"
RESOLVED = "resolved"
DISMISSED = "dismissed"
STATUSES = (OPEN, ACKNOWLEDGED, RESOLVED, DISMISSED)


def advisory_id(service: str, dependency: str, family_slug: str) -> str:
    """The identity of an exposure. Changing this orphans existing advisories."""
    return f"{service}:{dependency}:{family_slug}"


# Raising one advisory.
#
# ON MATCH is where the two status rules live:
#
#   resolved  -> reopen. The team fixed it, the family came back, they need to
#                know again.
#   dismissed -> leave alone. A human decided this does not apply to them.
#                Reopening would re-nag someone who already made a judgement
#                call, and a tool that does that gets ignored entirely.
#
# `reopened_at` is set BEFORE `status`, because it tests the OLD status. The
# ordering of SET items is load-bearing, which is why a test resolves an
# advisory and re-raises it rather than trusting this comment.
RAISE_QUERY = """
MATCH (svc:Service {name: $service})
MATCH (dep:Dependency {name: $dependency})
MERGE (a:Advisory {id: $id})
  ON CREATE SET
    a.status       = $open,
    a.family       = $family,
    a.family_slug  = $family_slug,
    a.service      = $service,
    a.dependency   = $dependency,
    a.raised_at    = $now,
    a.last_seen_at = $now,
    a.seen_count   = 1
  ON MATCH SET
    a.seen_count   = coalesce(a.seen_count, 1) + 1,
    a.last_seen_at = $now,
    a.reopened_at  = CASE WHEN a.status = $resolved THEN $now ELSE a.reopened_at END,
    a.status       = CASE WHEN a.status = $resolved THEN $open ELSE a.status END
MERGE (a)-[:WARNS]->(svc)
MERGE (a)-[:VIA]->(dep)
WITH a
// Evidence accumulates: each new triggering incident adds an edge, so "why am
// I being told this" can always be answered, and how often it has recurred is
// countable rather than a number someone incremented by hand.
UNWIND $because_of AS incident_id
MATCH (inc:Incident {id: incident_id})
MERGE (a)-[:BECAUSE_OF]->(inc)
RETURN a.id AS id, a.status AS status, a.seen_count AS seen_count
"""

# The fix to try and the people to tell. Separate from RAISE_QUERY because
# these are MATCHes that may find nothing — an exposure with no recorded fix
# is still a real exposure, and must not be dropped because a MATCH failed.
ATTACH_QUERY = """
MATCH (a:Advisory {id: $id})
OPTIONAL MATCH (:Incident {id: $suggest_from})-[:RESOLVED_BY]->(res:Resolution)
FOREACH (r IN CASE WHEN res IS NULL THEN [] ELSE [res] END |
  MERGE (a)-[:SUGGESTS]->(r)
)
WITH a
OPTIONAL MATCH (:Service {name: $service})-[:OWNED_BY]->(team:Team)
FOREACH (t IN CASE WHEN team IS NULL THEN [] ELSE [team] END |
  MERGE (a)-[:ROUTED_TO]->(t)
)
WITH a
OPTIONAL MATCH (eng:Engineer {handle: $ask})
FOREACH (e IN CASE WHEN eng IS NULL THEN [] ELSE [eng] END |
  MERGE (a)-[:SUGGESTED_ASK]->(e)
)
RETURN a.id AS id
"""

LIST_QUERY = """
MATCH (a:Advisory)-[:WARNS]->(svc:Service)
WHERE ($status IS NULL OR a.status = $status)
OPTIONAL MATCH (a)-[:ROUTED_TO]->(team:Team)
WITH a, svc, team
WHERE ($team IS NULL OR team.name = $team)
OPTIONAL MATCH (a)-[:VIA]->(dep:Dependency)
OPTIONAL MATCH (a)-[:BECAUSE_OF]->(inc:Incident)
OPTIONAL MATCH (a)-[:SUGGESTS]->(res:Resolution)
OPTIONAL MATCH (a)-[:SUGGESTED_ASK]->(eng:Engineer)
RETURN a.id AS id, a.status AS status, a.family AS family,
       svc.name AS service, collect(DISTINCT dep.name) AS via,
       collect(DISTINCT inc.id) AS because_of,
       res.action AS suggested_fix, eng.handle AS ask,
       team.name AS routed_to, a.seen_count AS seen_count,
       a.raised_at AS raised_at, a.last_seen_at AS last_seen_at,
       a.reopened_at AS reopened_at
ORDER BY a.last_seen_at DESC, a.id
"""

SET_STATUS_QUERY = """
MATCH (a:Advisory {id: $id})
SET a.status = $status, a.status_changed_at = $now
RETURN a.id AS id, a.status AS status
"""


@dataclass
class Advisory:
    id: str
    status: str
    family: str
    service: str
    via: List[str] = field(default_factory=list)
    because_of: List[str] = field(default_factory=list)
    suggested_fix: Optional[str] = None
    ask: Optional[str] = None
    routed_to: Optional[str] = None
    seen_count: int = 1
    raised_at: Optional[str] = None
    last_seen_at: Optional[str] = None
    reopened_at: Optional[str] = None

    def why(self) -> str:
        """An advisory has to justify itself as much as a match does."""
        bits = []
        if self.via:
            bits.append(f"shares {', '.join(sorted(self.via))}")
        if self.because_of:
            bits.append(f"after {', '.join(sorted(self.because_of))}")
        if self.seen_count > 1:
            bits.append(f"seen {self.seen_count} times")
        return "; ".join(bits) or "no evidence"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def raise_advisories(graph, alert, matches, peers, now: str = None) -> List[Advisory]:
    """Raise one advisory per exposed peer, and return what is now open.

    A peer is exposed when it shares a dependency with the alerting service.
    Peers that have already had this family of incident are skipped: they do
    not need warning about something they have lived through, and telling them
    anyway is how a useful signal turns into noise.
    """
    now = now or _now()
    if not matches:
        return []

    family_slug = slug_of(alert.tags)
    # The best match is the evidence and the source of the suggested fix.
    best = matches[0]
    because_of = [m.id for m in matches[:3]]

    raised = []
    for peer in peers:
        if int(peer["past_incidents"]) > 0:
            continue
        for dependency in peer["via"]:
            aid = advisory_id(peer["service"], dependency, family_slug)
            graph.query(
                RAISE_QUERY,
                params={
                    "id": aid,
                    "service": peer["service"],
                    "dependency": dependency,
                    "family": alert_family(alert),
                    "family_slug": family_slug,
                    "because_of": because_of,
                    "now": now,
                    "open": OPEN,
                    "resolved": RESOLVED,
                },
            )
            routing = route(graph, peer["service"], family_slug)
            ask = routing["ask_first"].engineer if routing["ask_first"] else None
            graph.query(
                ATTACH_QUERY,
                params={
                    "id": aid,
                    "service": peer["service"],
                    "suggest_from": best.id,
                    "ask": ask,
                },
            )
            raised.append(aid)

    return [a for a in list_advisories(graph) if a.id in raised]


def alert_family(alert) -> str:
    from dejavu.families import family_of

    return family_of(alert.tags)


def list_advisories(graph, status: str = None, team: str = None) -> List[Advisory]:
    rows = _rows(graph.ro_query(LIST_QUERY, params={"status": status, "team": team}))
    return [
        Advisory(
            id=r["id"],
            status=r["status"],
            family=r["family"],
            service=r["service"],
            via=sorted(r["via"] or []),
            because_of=sorted(r["because_of"] or []),
            suggested_fix=r["suggested_fix"],
            ask=r["ask"],
            routed_to=r["routed_to"],
            seen_count=int(r["seen_count"] or 1),
            raised_at=r["raised_at"],
            last_seen_at=r["last_seen_at"],
            reopened_at=r["reopened_at"],
        )
        for r in rows
    ]


def set_status(graph, advisory_id: str, status: str, now: str = None) -> Optional[str]:
    """Acknowledge, resolve or dismiss an advisory."""
    if status not in STATUSES:
        raise ValueError(f"unknown status {status!r}; expected one of {STATUSES}")
    rows = _rows(
        graph.query(
            SET_STATUS_QUERY,
            params={"id": advisory_id, "status": status, "now": now or _now()},
        )
    )
    return rows[0]["status"] if rows else None
