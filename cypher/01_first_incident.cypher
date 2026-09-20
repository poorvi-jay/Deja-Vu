// Déjà Vu — Phase 1, first incident loaded by hand.
// Run with: .venv\Scripts\python.exe scripts\run_cypher.py cypher/01_first_incident.cypher
//
// Every write in this file is a MERGE, so the whole file is idempotent:
// running it ten times leaves the graph exactly as running it once does.
// MERGE = "find a node matching the braces, or create it if absent."
//
// The properties inside the braces are the node's IDENTITY — they are what
// MERGE searches on. Everything else goes in ON CREATE SET, which only runs
// on the first load and so never overwrites data already in the graph.

// ---------------------------------------------------------------------------
// 1. The shared nodes first, so the incident has something to attach to.
// ---------------------------------------------------------------------------

MERGE (svc:Service {name: 'checkout-api'})
  ON CREATE SET svc.team = 'payments', svc.language = 'java'

MERGE (dep:Dependency {name: 'HikariCP', version: '5.0.1'})
// This is the node that earns us the graph. "Which other services could hit
// this?" is answered by walking Service<-[:DEPENDS_ON]-...->Dependency, and
// that fact appears in no incident's text.

MERGE (svc)-[:DEPENDS_ON]->(dep)

MERGE (t1:Tag {name: 'connection-pool'})
MERGE (t2:Tag {name: 'timeout'})
MERGE (t3:Tag {name: 'database'})
// Tags as nodes, not a list property. Two incidents sharing 'timeout' are now
// two hops apart: (i1)-[:TAGGED]->(:Tag)<-[:TAGGED]-(i2).

// ---------------------------------------------------------------------------
// 2. The incident itself.
// ---------------------------------------------------------------------------

MERGE (inc:Incident {id: 'INC-001'})
  ON CREATE SET
    inc.title       = 'checkout-api 504s during evening peak',
    inc.occurred_at = '2025-08-14T19:05:00Z',
    inc.severity    = 'SEV2',
    inc.summary     = 'Checkout requests timed out for 38 minutes. HikariCP pool was exhausted; connections were being held by a slow analytics query on the shared primary.'

// The error signature: this incident has one, but other incidents can share
// the same normalised text — which is exactly how the agent will find them.
// The text itself is the identity, so it goes in the braces.
MERGE (sig:ErrorSignature {
  normalized: 'HikariPool-1 - Connection is not available, request timed out after Nms'
})

MERGE (inc)-[:AFFECTED]->(svc)
MERGE (inc)-[:HAS_SIGNATURE]->(sig)
MERGE (inc)-[:TAGGED]->(t1)
MERGE (inc)-[:TAGGED]->(t2)
MERGE (inc)-[:TAGGED]->(t3)
// MERGE on a relationship looks for that exact edge between those two exact
// nodes. Both endpoints are already bound by the clauses above, so this finds
// the existing edge on a re-run instead of adding a parallel one.

// ---------------------------------------------------------------------------
// 3. The resolution — what the agent will actually hand back to an on-call SRE.
// ---------------------------------------------------------------------------

// A resolution has no natural identity of its own: two incidents could
// plausibly have the same action text and the same fixer. So we derive its id
// from the incident it belongs to.
MERGE (res:Resolution {id: 'INC-001-res'})
  ON CREATE SET
    res.action              = 'Raised HikariCP maximumPoolSize 10 -> 30 and moved the analytics query to the read replica.',
    res.fixed_by            = 'priya.n',
    res.time_to_resolve_min = 38

MERGE (inc)-[:RESOLVED_BY]->(res)

RETURN inc.id AS loaded;
