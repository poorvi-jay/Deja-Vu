"""Find past incidents that resemble a new alert.

The graph offers three independent kinds of evidence, and this module's job is
to weigh them against each other:

  1. Same error signature   - one hop through :ErrorSignature. Near-conclusive.
  2. Shared tags            - two hops through :Tag. More overlap, more similar.
  3. Same service, or a service sharing a dependency - weakest, but it catches
     incidents whose error text reads completely differently.

Nothing here is machine learning. The weights below are a judgement call,
written down in one place so they can be argued with and changed.
"""

from dataclasses import dataclass, field
from typing import List, Optional

from dejavu.normalize import normalize

# How much each kind of evidence is worth. Tuned so that one signature match
# (10) outranks any realistic tag overlap, since two incidents raising the
# identical error are almost always the same failure; but three shared tags
# (9) plus the same service (4) can still beat it, which is what lets a
# differently-worded incident such as INC-019 surface.
SIGNATURE_WEIGHT = 10
TAG_WEIGHT = 3
SAME_SERVICE_WEIGHT = 4
SHARED_DEPENDENCY_WEIGHT = 2

# How many tags must overlap before tags alone qualify a match. See the note
# in SIMILAR_QUERY: one generic tag is not evidence.
MIN_SHARED_TAGS = 2


@dataclass
class Alert:
    """A new incident, as it arrives from an on-call engineer or a pager."""

    service: str
    error: str
    tags: List[str] = field(default_factory=list)

    @property
    def signature(self) -> str:
        """The error text in the same form the graph stores it."""
        return normalize(self.error)


@dataclass
class Match:
    """One past incident, with the evidence that made it a match."""

    id: str
    title: str
    severity: str
    occurred_at: str
    service: str
    score: int
    signature_match: bool
    shared_tags: List[str]
    shared_dependencies: List[str]
    same_service: bool
    resolution: Optional[str]
    fixed_by: Optional[str]

    def why(self) -> str:
        """A plain-language account of why this incident was returned.

        Only the evidence that actually qualified the match appears here.
        Shared dependencies deliberately do not: the query counts ANY library
        two services have in common, so citing one implies a causal link the
        graph cannot vouch for. A Redis incident on a service that happens to
        share a connection-pool library would read as "shares HikariCP", which
        an engineer would reasonably take as a claim about the cause. Those go
        in `also_shares()` instead, stated as the plain fact they are.
        """
        reasons = []
        if self.signature_match:
            reasons.append("identical error signature")
        if self.shared_tags:
            reasons.append(f"shared tags: {', '.join(sorted(self.shared_tags))}")
        if self.same_service:
            reasons.append("same service")
        return "; ".join(reasons) or "no evidence"

    def also_shares(self) -> str:
        """Dependencies in common with the alerting service — context, not a
        reason. Empty when it is the same service, where it says nothing."""
        if self.same_service or not self.shared_dependencies:
            return ""
        return ", ".join(sorted(self.shared_dependencies))


# One query gathers all three kinds of evidence. Each OPTIONAL MATCH adds a
# column without dropping candidates that fail it — a plain MATCH would throw
# away every incident that lacked, say, a shared tag, and those are exactly
# the ones the signature match is meant to find.
SIMILAR_QUERY = """
MATCH (cand:Incident)-[:AFFECTED]->(csvc:Service)

OPTIONAL MATCH (cand)-[:HAS_SIGNATURE]->(sig:ErrorSignature {normalized: $signature})
WITH cand, csvc, CASE WHEN sig IS NULL THEN 0 ELSE 1 END AS sig_match

OPTIONAL MATCH (cand)-[:TAGGED]->(t:Tag)
WHERE t.name IN $tags
WITH cand, csvc, sig_match,
     collect(DISTINCT t.name) AS shared_tags

OPTIONAL MATCH (csvc)-[:DEPENDS_ON]->(d:Dependency)<-[:DEPENDS_ON]-(:Service {name: $service})
WITH cand, csvc, sig_match, shared_tags,
     collect(DISTINCT d.name) AS shared_deps

WITH cand, csvc, sig_match, shared_tags, shared_deps,
     CASE WHEN csvc.name = $service THEN 1 ELSE 0 END AS same_service

WITH cand, csvc, sig_match, shared_tags, shared_deps, same_service,
     sig_match * $signature_weight
     + size(shared_tags) * $tag_weight
     + same_service * $same_service_weight
     + CASE WHEN size(shared_deps) > 0 THEN $shared_dependency_weight ELSE 0 END
     AS score
// What counts as enough evidence to qualify at all.
//
// A shared dependency never qualifies a candidate on its own, or an
// unrecognised error on refund-service would return every Kafka incident that
// happened to a service also using HikariCP. It only adjusts the ranking of
// candidates that already qualified.
//
// One shared tag is not enough either. Generic tags like `timeout` sit on a
// fifth of the incidents, so a single overlap is close to meaningless: it let
// a Redis hot-key incident surface against a connection-pool alert. Two tags
// is the bar, which still admits a differently-worded incident like INC-019
// that shares three.
WHERE sig_match > 0 OR same_service > 0 OR size(shared_tags) >= $min_shared_tags

OPTIONAL MATCH (cand)-[:RESOLVED_BY]->(res:Resolution)
RETURN cand.id AS id, cand.title AS title, cand.severity AS severity,
       cand.occurred_at AS occurred_at, csvc.name AS service,
       score, sig_match, shared_tags, shared_deps, same_service,
       res.action AS resolution, res.fixed_by AS fixed_by
ORDER BY score DESC, cand.occurred_at DESC
LIMIT $limit
"""


# "Who else could hit this?" This is the question a vector search over
# incident text cannot answer: the shared dependency is an edge in the graph,
# and it appears nowhere in any incident's wording.
AT_RISK_QUERY = """
MATCH (:Service {name: $service})-[:DEPENDS_ON]->(d:Dependency)<-[:DEPENDS_ON]-(peer:Service)
WHERE peer.name <> $service
OPTIONAL MATCH (peer)<-[:AFFECTED]-(i:Incident)-[:TAGGED]->(t:Tag)
WHERE t.name IN $tags
WITH peer, collect(DISTINCT d.name) AS via, count(DISTINCT i) AS past_incidents
RETURN peer.name AS service, via, past_incidents
ORDER BY past_incidents DESC, service
"""


def find_similar(graph, alert: Alert, limit: int = 5) -> List[Match]:
    """Return past incidents resembling `alert`, best match first."""
    result = graph.ro_query(
        SIMILAR_QUERY,
        params={
            "signature": alert.signature,
            "tags": alert.tags,
            "service": alert.service,
            "limit": limit,
            "signature_weight": SIGNATURE_WEIGHT,
            "tag_weight": TAG_WEIGHT,
            "same_service_weight": SAME_SERVICE_WEIGHT,
            "shared_dependency_weight": SHARED_DEPENDENCY_WEIGHT,
            "min_shared_tags": MIN_SHARED_TAGS,
        },
    )
    columns = [c[1] if isinstance(c, (list, tuple)) else c for c in result.header]
    matches = []
    for row in result.result_set:
        r = dict(zip(columns, row))
        matches.append(
            Match(
                id=r["id"],
                title=r["title"],
                severity=r["severity"],
                occurred_at=r["occurred_at"],
                service=r["service"],
                score=int(r["score"]),
                signature_match=bool(r["sig_match"]),
                shared_tags=list(r["shared_tags"] or []),
                shared_dependencies=list(r["shared_deps"] or []),
                same_service=bool(r["same_service"]),
                resolution=r["resolution"],
                fixed_by=r["fixed_by"],
            )
        )
    return matches


def at_risk_services(graph, alert: Alert):
    """Services sharing a dependency with the alerting one.

    `past_incidents` counts how many of that peer's own incidents carry the
    alert's tags: a high count means the peer has hit this before and may hold
    the fix, a zero means it is exposed but has not been bitten yet.
    """
    result = graph.ro_query(
        AT_RISK_QUERY, params={"service": alert.service, "tags": alert.tags}
    )
    columns = [c[1] if isinstance(c, (list, tuple)) else c for c in result.header]
    return [dict(zip(columns, row)) for row in result.result_set]
