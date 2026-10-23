# Déjà Vu

**Live: https://deja-vu-hfbi.onrender.com**

A graph-backed incident memory. When a service pages, Déjà Vu answers two
questions from an incident history stored in FalkorDB:

- **"Have we seen this before?"** — past incidents ranked by how they resemble
  the alert, each with the evidence that made it a match and the fix that
  worked.
- **"Who else could hit this?"** — other services exposed through a shared
  dependency, including ones that have never had an incident.

The second question is the reason this is a graph and not a text search. That
two services share a connection-pool library is an *edge*; it appears in the
wording of no incident, so no amount of searching the text can find it.

## Submission

**Graph Hacks — Track: Agent Memory and Coordination**

| | |
|---|---|
| Live deployment | https://deja-vu-hfbi.onrender.com |
| Graph database | FalkorDB Cloud (free tier) |
| Data | synthetic — 27 incidents written for this project, no real or private data |

### Why the graph is load-bearing

Take FalkorDB out and the product does not degrade, it loses an answer it
cannot produce any other way. "Which other services could hit this?" is
answered by walking `Incident → Service → Dependency ← Service`. That
`refund-service` shares HikariCP 5.0.1 with three services that have each hit
pool exhaustion is written in no incident's text — nobody records "by the way,
refund-service uses the same connection pool." It exists only as a path. A
vector store over the same incident text can rank similar *wording*; it cannot
traverse a relationship that was never written down.

The ranking itself is also a traversal, not a text scan: shared tags are
`(a)-[:TAGGED]->(:Tag)<-[:TAGGED]-(b)`, two hops, which is how INC-019 is
found despite its error text (`SQLSTATE(08006)`) sharing no words with the
alert.

### AI assistance disclosure

This project was built with **Claude Code** (Anthropic) as a coding assistant,
used throughout: the schema was designed interactively, and the retrieval
scoring, agent loop, HTTP API, budget cap, loader, evaluation harness and
tests were written with its help and reviewed by me. The scoring weights,
qualification thresholds and schema decisions are documented in this README
and in comments at each decision point, including the two cases where the
evaluation harness disproved an earlier rule.

## Setup

```
python -m venv .venv
.venv\Scripts\pip.exe install -r requirements.txt
copy .env.example .env
```

Then put your FalkorDB connection string in `.env`. `.env` is gitignored.

## Loading the graph

```
.venv\Scripts\python.exe scripts\load_incidents.py
```

Reads `data/incidents.json` and writes 125 nodes and 184 relationships. Every
write is a `MERGE`, so the load is idempotent — a second run reports zero
created and the graph does not drift.

## Asking a question

```
.venv\Scripts\python.exe scripts\find_similar.py ^
  --service refund-service ^
  --error "HikariPool-1 - Connection is not available, request timed out after 30000ms." ^
  --tags connection-pool,timeout,database
```

`refund-service` has no incident history of its own. The graph still returns
five past pool exhaustions from three other services, with their fixes, plus
the three peers sharing HikariCP.

## The schema

```
(:Incident)-[:AFFECTED]->(:Service)-[:DEPENDS_ON]->(:Dependency)
(:Incident)-[:HAS_SIGNATURE]->(:ErrorSignature)
(:Incident)-[:TAGGED]->(:Tag)
(:Incident)-[:RESOLVED_BY]->(:Resolution)-[:FIXED_BY]->(:Engineer)-[:MEMBER_OF]->(:Team)
(:Service)-[:OWNED_BY]->(:Team)
(:Advisory)-[:WARNS]->(:Service)
(:Advisory)-[:VIA]->(:Dependency)
(:Advisory)-[:BECAUSE_OF]->(:Incident)
(:Advisory)-[:SUGGESTS]->(:Resolution)
(:Advisory)-[:ROUTED_TO]->(:Team)
(:Advisory)-[:SUGGESTED_ASK]->(:Engineer)
```

The design rule behind it: **you can walk to a node, you cannot walk to a
property.** Anything two incidents might share is a node, so the shared thing
becomes a path between them. Tags are nodes rather than a list property for
exactly this reason — two incidents sharing a tag are two hops apart.

## How matching works

`dejavu/retrieve.py` weighs three independent kinds of evidence:

| Evidence | Weight | Hops | Role |
|---|---|---|---|
| Identical error signature | 10 | 1, via `:ErrorSignature` | qualifies |
| Each shared tag | 3 | 2, via `:Tag` | qualifies at 2+ |
| Same service | 4 | 1, via `:AFFECTED` | boost only |
| Shared dependency | 2 | 3, via `:Dependency` | boost only |

To qualify at all, a candidate needs an identical signature or **at least
two** shared tags. Same service and shared dependency adjust the ranking of
what already qualified; neither admits a candidate on its own.

That rule was tightened twice, both times because something unrelated came
back. One shared tag used to be enough, and generic tags like `timeout` sit on
a fifth of the incidents — a Redis hot-key incident surfaced against a
connection-pool alert. Then the evaluation harness caught the other half: an
unrecognised crash on `payment-gateway` returned that service's two TLS
incidents, because sharing a service was enough. *What else broke here* is a
different question from *have we seen this*, and this endpoint answers the
second.

For the same reason, a shared dependency is never given as a *reason* a match
was returned. The query counts any library two services have in common, so
naming one would imply a causal link the graph cannot vouch for. It is
reported separately as context.

The weights are a judgement call, not a trained model, which is the point:
every result explains itself in terms an engineer can check and disagree with.

### Normalisation

Errors never repeat verbatim — the timeout is `30000ms` one day and `5000ms`
the next. `dejavu/normalize.py` reduces both to `Nms`. The rule that makes it
safe is that the **same function runs on both sides**: on stored signatures as
the loader writes them, and on alerts as retrieval looks them up. A test
asserts that every stored signature is already in normalised form, so the two
sides cannot drift apart.

Numbers with fewer than six digits are left alone, because the `1` in
`HikariPool-1`, the `509` in `x509`, HTTP `404` and the SQLSTATE code in
`SQLSTATE(08006)` are identity, not noise.

## HTTP API

```
.venv\Scripts\python.exe -m uvicorn dejavu.api:app --reload
```

The demo UI is at `http://127.0.0.1:8000/` and interactive API docs at
`http://127.0.0.1:8000/docs`.

| Endpoint | Purpose |
|---|---|
| `GET /health` | Up, connected, and how many incidents are loaded |
| `POST /triage` | An alert in; ranked matches, a briefing, and exposed peers out |
| `GET /incidents` | Every incident, newest first, with its root-cause family |
| `GET /incidents/{id}` | One incident in full |
| `GET /services` | Every service with its dependencies and incident count |
| `GET /budget` | Model spend so far against the hard cap |

Each match in a `/triage` response carries a `breakdown` of its score by
weight (signature, tags, same service, dependency), which always sums to the
score, so a client can show the arithmetic rather than a bare number.

The data has no family field, so `/incidents` derives one from tags: the first
of six families whose tags an incident carries. The order is deliberate —
INC-023 is tagged `deploy` and `connection-pool`, and it belongs with the pool
incidents.

```
curl -X POST http://127.0.0.1:8000/triage -H "Content-Type: application/json" ^
  -d "{\"service\":\"refund-service\",\"error\":\"HikariPool-1 - Connection is not available, request timed out after 30000ms.\",\"tags\":[\"connection-pool\",\"timeout\"]}"
```

`/health` checks the incident count, not just the connection, so an empty
database reports `degraded` instead of passing while answering every query with
nothing. A database that is unreachable gives `503`, not `500` — a paused
FalkorDB Cloud instance is a dependency being down, not a bug in this service.

## Tests

```
.venv\Scripts\python.exe -m pytest
```

137 tests. The integration tests run against the live graph; they skip only
if no database is reachable. The model explainer is verified with a fake
client, so the suite needs no API key and costs nothing to run.

## Triage

```
.venv\Scripts\python.exe scripts\triage.py ^
  --service refund-service ^
  --error "HikariPool-1 - Connection is not available, request timed out after 30000ms." ^
  --tags connection-pool,timeout,database
```

The loop is `normalize -> retrieve -> explain`, and the model only does the
last step. It never queries the graph and never decides what is relevant; it is
handed the incidents retrieval found and asked to write them up. So a wrong
answer is either a retrieval bug — reproducible and covered by tests — or a
writing problem, and the two are never tangled together.

Two explainers implement the same interface:

- `TemplateExplainer` (default) — deterministic, no API key, no network.
- `OpenAIExplainer` (`--explainer openai`) — needs `OPENAI_API_KEY` in `.env`.
  Defaults to `gpt-4o-mini`; override with `--model`.

The model sits behind a protocol, so the provider is a swappable detail and
what the model is told is not. An Anthropic-backed explainer lived here and
was removed: it was never run against the real API, and an untested path is
worse than an absent one.

`--show-evidence` prints the exact text the explainer was given. Anything in a
briefing that is not in that text is fabricated, which makes that failure easy
to spot.

## The queries the product depends on

Three queries do the work. All values arrive as `$parameters` — never pasted
into the query text — so an apostrophe in a summary cannot break the Cypher
and the database reuses one plan.

### 1. Ranked retrieval — `dejavu/retrieve.py`

Each `OPTIONAL MATCH` adds a column of evidence without dropping candidates
that fail it. A plain `MATCH` would discard every incident lacking, say, a
shared tag — exactly the ones the signature match exists to find.

```cypher
MATCH (cand:Incident)-[:AFFECTED]->(csvc:Service)

OPTIONAL MATCH (cand)-[:HAS_SIGNATURE]->(sig:ErrorSignature {normalized: $signature})
WITH cand, csvc, CASE WHEN sig IS NULL THEN 0 ELSE 1 END AS sig_match

OPTIONAL MATCH (cand)-[:TAGGED]->(t:Tag)
WHERE t.name IN $tags
WITH cand, csvc, sig_match, collect(DISTINCT t.name) AS shared_tags

OPTIONAL MATCH (csvc)-[:DEPENDS_ON]->(d:Dependency)<-[:DEPENDS_ON]-(:Service {name: $service})
WITH cand, csvc, sig_match, shared_tags, collect(DISTINCT d.name) AS shared_deps

WITH cand, csvc, sig_match, shared_tags, shared_deps,
     CASE WHEN csvc.name = $service THEN 1 ELSE 0 END AS same_service

WITH cand, csvc, sig_match, shared_tags, shared_deps, same_service,
     sig_match * $signature_weight
     + size(shared_tags) * $tag_weight
     + same_service * $same_service_weight
     + CASE WHEN size(shared_deps) > 0 THEN $shared_dependency_weight ELSE 0 END
     AS score
WHERE sig_match > 0 OR size(shared_tags) >= $min_shared_tags

OPTIONAL MATCH (cand)-[:RESOLVED_BY]->(res:Resolution)
RETURN cand.id AS id, cand.title AS title, cand.severity AS severity,
       cand.occurred_at AS occurred_at, csvc.name AS service,
       score, sig_match, shared_tags, shared_deps, same_service,
       res.action AS resolution, res.fixed_by AS fixed_by
ORDER BY score DESC, cand.occurred_at DESC
LIMIT $limit
```

### 2. Who else is exposed — the query no text search can replace

A three-hop traversal out of the alerting service, through a shared
dependency, into its peers. `past_incidents` separates peers that have hit
this before (they hold the fix) from peers that are exposed and have not been
bitten yet.

```cypher
MATCH (:Service {name: $service})-[:DEPENDS_ON]->(d:Dependency)<-[:DEPENDS_ON]-(peer:Service)
WHERE peer.name <> $service
OPTIONAL MATCH (peer)<-[:AFFECTED]-(i:Incident)-[:TAGGED]->(t:Tag)
WHERE t.name IN $tags
WITH peer, collect(DISTINCT d.name) AS via, count(DISTINCT i) AS past_incidents
RETURN peer.name AS service, via, past_incidents
ORDER BY past_incidents DESC, service
```

### 3. Idempotent load — `dejavu/loader.py`

Every write is a `MERGE`, so loading twice changes nothing. The braces hold
identity only; everything else goes in `ON CREATE SET` so a reload never
overwrites what is already stored.

```cypher
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
```

`UNWIND` goes last deliberately: unwinding an empty list yields zero rows, so
any clause after it would silently not run.

## Coordination

Retrieval answers "have we seen this before". Coordination answers what comes
next: who owns the exposed service, who has actually fixed this kind of
failure, and has anyone been told.

`team` and `fixed_by` used to be properties, so neither was reachable. They are
nodes now, for the same reason tags are: **you can walk to a node, you cannot
walk to a property.** `(:Engineer)` is keyed on handle, not handle+team —
people change teams, and a key that includes team would produce a second node
for one person the day they move.

That makes one query possible that was not before:

```cypher
MATCH (:Tag {name: $family})<-[:TAGGED]-(i:Incident)
      -[:RESOLVED_BY]->(:Resolution)-[:FIXED_BY]->(eng:Engineer)-[:MEMBER_OF]->(t:Team)
RETURN eng.handle, t.name, count(DISTINCT i) AS fixes, collect(DISTINCT i.id)
ORDER BY fixes DESC
```

For Kafka failures that answers `marcus.o` in **orders** — and `search-api`,
which is owned by **discovery**, is the service exposed to them. Routing names
both: the team is accountable and always exists, the expert may be three teams
away. Routing only to an individual breaks the week they are on leave;
routing only to a team throws away the thing the graph uniquely knows.

### Advisories

An advisory says "this has not happened to you yet, and it probably will". Its
identity is **(service, family)** — deliberately not the dependency, because
two implicated dependencies are one exposure with two reasons.

Because identity is a key, deduplication is a consequence of `MERGE` rather
than a check anyone maintains. Raise the same exposure three times and you get
one node with `seen_count: 3`.

```cypher
MERGE (a:Advisory {id: $id})
  ON CREATE SET a.status = $open, a.raised_at = $now, a.seen_count = 1
  ON MATCH SET  a.seen_count   = coalesce(a.seen_count, 1) + 1,
                a.last_seen_at = $now,
                a.reopened_at  = CASE WHEN a.status = $resolved THEN $now ELSE a.reopened_at END,
                a.status       = CASE WHEN a.status = $resolved THEN $open ELSE a.status END
```

**Resolved reopens, dismissed does not.** Resolved means "we fixed it", so a
recurrence is news. Dismissed means a human judged it inapplicable, and
re-nagging them is how a tool gets muted — but `seen_count` still climbs, so
the decision can be revisited with evidence. `reopened_at` is set *before*
`status` because it reads the old value; a test resolves and re-raises an
advisory rather than trusting that.

Two rules decide who gets warned, both learned by getting them wrong first:

- **The dependency must be implicated** — common to *every* service that has
  had this family. Without it, a cache alert warned a peer "you share
  HikariCP", a connection-pool library irrelevant to caches. Conservative by
  design: if a family's incidents share no dependency, nobody is warned.
- **A peer that has already had this family is not warned.** This first used
  `at_risk_services`, which counts any overlapping tag — so an incident
  sharing only `timeout` marked a peer as experienced in pool failures.

| Endpoint | Purpose |
|---|---|
| `POST /advisories` | Raise one per exposed peer. Idempotent. |
| `GET /advisories?status=&team=` | What is outstanding; `team` is "what is on my plate". |
| `PATCH /advisories/{id}` | acknowledge, resolve or dismiss |

Set `DEJAVU_WRITE_TOKEN` to require an `X-Write-Token` header on the two write
endpoints. Unset — the default — leaves them open, which is what the tests and
local development use.

### Is it right?

```
.venv\Scripts\python.exe scripts\evaluate.py
```

Advisories are checked against an **independent oracle**: who should be warned
is recomputed in plain Python from `data/incidents.json`, by a different method
than the Cypher under test. A query asserted against itself proves nothing.

| Check | Result |
|---|---|
| Agreement with the oracle | **14/14** cases |
| Idempotent | re-raising everything leaves the population unchanged |
| Duplicates | none |

The oracle earned its place immediately: it caught both of the rules above
being wrong, plus two duplicate bugs — `EXPOSED_QUERY` grouping by
`(peer, dependency)` so a peer exposed through two dependencies came back
twice, and `LIST_QUERY` grouping by `res.action` so an advisory that had
collected a second suggested fix came back as two rows.

Honest note on the data: the coordination feature fired in only one of
fourteen eval cases at first, because almost nothing in the data qualified. I
added the shared infrastructure the incident summaries already describe — a
shared Postgres primary and one Kafka cluster — plus `billing-worker`, a
service with no incident history. Four cases fire now, across two families.
The modelling is defensible (shared infrastructure causes correlated failure
more than client libraries do) but the richer result came from better data, not
better code.

## Does the ranking actually work?

```
.venv\Scripts\python.exe scripts\evaluate.py
```

13 held-out alerts, written as new occurrences of the six known root-cause
families — different timeouts, extra clauses real tools emit, and several with
no exact signature match so only tags can reach the family.

| Metric | Score | Question it answers |
|---|---|---|
| hit@1 | **100%** | Is the first result from the right family? |
| precision@3 | **100%** | Is the whole shortlist useful, not just the winner? |
| MRR | **1.00** | Is the right answer ever buried down the list? |
| negatives | **2/2** | Does an unrecognised alert return *nothing*? |

The negatives carry as much weight as the rest. A system that always answers
is not retrieving, it is guessing — and one of them caught a real bug, which
is how the same-service rule above got tightened. `precision@3` rose from 94%
to 100% as a result: the fix removed noise from the shortlists too.

No model is called, so a run is free, repeatable, and unaffected by changes in
the prose. `tests/test_evaluate.py` asserts these thresholds, so altering a
scoring weight tells you immediately whether you improved retrieval or broke
it.

Honest limitation: the alerts are hand-written from the same families the data
describes, so this measures whether retrieval ranks the right family — not
whether it generalises to phrasing nobody anticipated. Alerts drawn from real
production logs would be a stronger test.

## Model spend

Every model call is costed from the API's own reported token usage, written to
`.spend.json`, and **refused** once a cap is reached — `dejavu/budget.py`,
exposed at `GET /budget`. A briefing costs about **$0.000245** on
`gpt-4o-mini`. An unknown model is billed at the most expensive rate in the
table rather than zero, so a pricing gap fails in the direction that stops you
early.

The ledger is a file, so the cap survives the process exiting. On an ephemeral
host it resets on restart, which makes `DEJAVU_BUDGET_USD` a per-boot cap
there rather than a lifetime one.

## Deployment

One Render web service serves both the API and the UI — the static files are
mounted at `/`, so there is no second origin and no CORS to configure.

| Setting | Value |
|---|---|
| Build | `pip install -r requirements.txt` |
| Start | `uvicorn dejavu.api:app --host 0.0.0.0 --port $PORT` |
| Health check | `/health` |
| Env | `FALKOR_URL`, `FALKOR_GRAPH`, `OPENAI_API_KEY`, `DEJAVU_BUDGET_USD`, `PYTHON_VERSION` |

Two things keep the demo alive on free tiers, both of which had already
bitten:

- **Self-seeding.** If the API starts and finds an empty graph, it loads
  `data/incidents.json` itself. A free FalkorDB instance can be paused or
  removed between demos; the service heals rather than serving zero results.
  Disable with `DEJAVU_SEED_ON_START=0`.
- **Keepalive.** `.github/workflows/keepalive.yml` pings `/health` every ten
  minutes, which keeps the Render instance awake *and* counts as graph
  activity, since `/health` runs a real query. It fails the workflow run on a
  degraded graph, so the breakage surfaces before a visitor finds it.

`dejavu/static/vercel.json` optionally fronts the page with Vercel's CDN,
proxying the API paths back to Render so everything stays same-origin.

## Status

- **Phase 0** environment — done
- **Phase 1** schema and data, 27 incidents across 6 root-cause families — done
- **Phase 2** retrieval, scoring, at-risk services, CLI — done
- **Phase 3** agent loop — done. The OpenAI explainer has been run against
  the real API ($0.000245 per briefing)
- **Phase 4a** HTTP API — done
- **Phase 4b** demo UI — done. Plain HTML, CSS and JS in `dejavu/static/`,
  served by the API itself with no build step: triage with demo presets,
  incident catalog and detail, and the service dependency map
- **Phase 4c** evaluation harness — done
- **Phase 5** deployed on Render, with a self-seeding graph and a keepalive
  — done
- **Phase 6** demo video — recording is the last step
- **Coordination layer** — `:Team`, `:Engineer` and `:Advisory`, expertise
  routing, family-keyed dedupe, status transitions, an independently checked
  evaluation, and an advisories page in the UI — done
