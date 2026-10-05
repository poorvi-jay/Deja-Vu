# Déjà Vu

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
(:Incident)-[:RESOLVED_BY]->(:Resolution)
```

The design rule behind it: **you can walk to a node, you cannot walk to a
property.** Anything two incidents might share is a node, so the shared thing
becomes a path between them. Tags are nodes rather than a list property for
exactly this reason — two incidents sharing a tag are two hops apart.

## How matching works

`dejavu/retrieve.py` weighs three independent kinds of evidence:

| Evidence | Weight | Hops |
|---|---|---|
| Identical error signature | 10 | 1, via `:ErrorSignature` |
| Each shared tag | 3 | 2, via `:Tag` |
| Same service | 4 | 1, via `:AFFECTED` |
| Shared dependency | 2 | 3, via `:Dependency` |

To qualify at all, a candidate needs an identical signature, the same service,
or **at least two** shared tags.

A shared dependency only breaks ties. It never qualifies a candidate on its
own, or an unrecognised error would drag in every unrelated incident from
every service using the same library. Nor does one shared tag: generic tags
like `timeout` sit on a fifth of the incidents, so a single overlap let a
Redis hot-key incident surface against a connection-pool alert.

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

Interactive docs at `http://127.0.0.1:8000/docs`.

| Endpoint | Purpose |
|---|---|
| `GET /health` | Up, connected, and how many incidents are loaded |
| `POST /triage` | An alert in; ranked matches, a briefing, and exposed peers out |
| `GET /incidents/{id}` | One incident in full |
| `GET /services` | Every service with its dependencies and incident count |

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

61 tests. The integration tests run against the live graph; they skip only if
no database is reachable. The Claude request is verified with a fake client, so
the suite needs no API key and costs nothing to run.

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

Three explainers implement the same interface:

- `TemplateExplainer` (default) — deterministic, no API key, no network.
- `OpenAIExplainer` (`--explainer openai`) — needs `OPENAI_API_KEY` in `.env`.
  Defaults to `gpt-4o`; override with `--model`.
- `ClaudeExplainer` (`--explainer claude`) — needs `ANTHROPIC_API_KEY` in
  `.env` and `pip install anthropic`.

Both model explainers are handed the identical system prompt and the identical
evidence — a test asserts it. The provider is a swappable detail; what the
model is told is not.

`--show-evidence` prints the exact text the explainer was given. Anything in a
briefing that is not in that text is fabricated, which makes that failure easy
to spot.

## Status

- **Phase 0** environment — done
- **Phase 1** schema and data, 27 incidents across 6 root-cause families — done
- **Phase 2** retrieval, scoring, at-risk services, CLI — done
- **Phase 3** agent loop — done with the template explainer; the Claude
  explainer is written and unit-tested against a fake client, but has never
  been run against the real API
- **Phase 4a** HTTP API — done
- **Phase 4b** demo UI — not started
- **Phase 4c** evaluation harness — not started
