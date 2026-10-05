"""HTTP API for Déjà Vu.

    uvicorn dejavu.api:app --reload

Endpoints:
    GET  /health           is the service up, and can it reach the graph
    POST /triage           an alert in, ranked incidents + a briefing out
    GET  /incidents/{id}   one incident in full
    GET  /services         every service, with its dependencies

The route handlers are thin on purpose. All the logic lives in retrieve.py and
agent.py, which are tested without HTTP; these functions only translate between
JSON and those calls. Anything worth testing should be testable without a
server.
"""

import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import List, Literal, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from dejavu.agent import ClaudeExplainer, OpenAIExplainer, TemplateExplainer, triage
from dejavu.retrieve import Alert, get_incident

# The connection is opened once at startup and reused. Opening one per request
# would add a round trip to every call and exhaust the connection pool under
# any real load.
state: dict = {"graph": None, "error": None}


@asynccontextmanager
async def lifespan(app: FastAPI):
    from run_cypher import connect, load_env

    try:
        load_env()
        state["graph"] = connect()
        state["graph"].ro_query("RETURN 1")
    except BaseException as exc:
        # Startup must not crash the process: /health should come up and
        # report the problem, which is far easier to debug on a deployed box
        # than a container that exits before it logs anything. load_env calls
        # sys.exit on a missing .env, hence BaseException.
        state["error"] = str(exc) or exc.__class__.__name__
    yield


app = FastAPI(
    title="Déjà Vu",
    description="Graph-backed incident memory: what happened last time, and who else is exposed.",
    version="0.1.0",
    lifespan=lifespan,
)


def require_graph():
    """503 rather than 500 when the database is unreachable.

    A paused FalkorDB Cloud instance is a dependency being down, not a bug in
    this service, and the status code should say so.
    """
    if state["graph"] is None:
        raise HTTPException(
            status_code=503,
            detail=f"graph unavailable: {state['error'] or 'not connected'}",
        )
    return state["graph"]


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class TriageRequest(BaseModel):
    service: str = Field(..., examples=["refund-service"])
    error: str = Field(
        ...,
        examples=["HikariPool-1 - Connection is not available, request timed out after 30000ms."],
    )
    tags: List[str] = Field(default_factory=list, examples=[["connection-pool", "timeout"]])
    limit: int = Field(5, ge=1, le=50)
    explainer: Literal["template", "openai", "claude"] = "template"


class MatchOut(BaseModel):
    id: str
    title: str
    severity: str
    occurred_at: str
    service: str
    score: int
    why: str
    signature_match: bool
    shared_tags: List[str]
    also_shares: List[str]
    resolution: Optional[str]
    fixed_by: Optional[str]


class PeerOut(BaseModel):
    service: str
    via: List[str]
    past_incidents: int


class TriageResponse(BaseModel):
    service: str
    normalised_signature: str
    summary: str
    matches: List[MatchOut]
    at_risk: List[PeerOut]


class IncidentOut(BaseModel):
    id: str
    title: str
    severity: str
    occurred_at: str
    service: str
    summary: str
    error_signature: Optional[str]
    tags: List[str]
    resolution: Optional[str]
    fixed_by: Optional[str]
    time_to_resolve_min: Optional[int]


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/health")
def health():
    """Reports the graph's state and how many incidents are loaded.

    Checking the incident count, not just the connection, means an empty
    database shows up as degraded instead of passing a health check while
    answering every query with nothing.
    """
    if state["graph"] is None:
        return {"status": "degraded", "graph": "unavailable", "detail": state["error"]}
    try:
        rows = state["graph"].ro_query("MATCH (i:Incident) RETURN count(i)").result_set
        count = int(rows[0][0])
    except Exception as exc:
        return {"status": "degraded", "graph": "error", "detail": str(exc)}
    return {
        "status": "ok" if count else "degraded",
        "graph": "connected",
        "incidents": count,
    }


def build_explainer(choice: str):
    if choice == "template":
        return TemplateExplainer()
    cls, env_var = {
        "openai": (OpenAIExplainer, "OPENAI_API_KEY"),
        "claude": (ClaudeExplainer, "ANTHROPIC_API_KEY"),
    }[choice]
    if not os.environ.get(env_var):
        # 400, not 500: the caller asked for something this deployment cannot
        # do, and the message says exactly what is missing.
        raise HTTPException(
            status_code=400,
            detail=f"explainer '{choice}' needs {env_var} to be set on the server",
        )
    try:
        return cls()
    except ImportError:
        raise HTTPException(
            status_code=400, detail=f"explainer '{choice}' is not installed on the server"
        )


@app.post("/triage", response_model=TriageResponse)
def post_triage(request: TriageRequest):
    graph = require_graph()
    alert = Alert(service=request.service, error=request.error, tags=request.tags)
    result = triage(graph, alert, build_explainer(request.explainer), limit=request.limit)

    return TriageResponse(
        service=alert.service,
        normalised_signature=alert.signature,
        summary=result["summary"],
        matches=[
            MatchOut(
                id=m.id,
                title=m.title,
                severity=m.severity,
                occurred_at=m.occurred_at,
                service=m.service,
                score=m.score,
                why=m.why(),
                signature_match=m.signature_match,
                shared_tags=sorted(m.shared_tags),
                also_shares=sorted(m.shared_dependencies) if not m.same_service else [],
                resolution=m.resolution,
                fixed_by=m.fixed_by,
            )
            for m in result["matches"]
        ],
        at_risk=[
            PeerOut(
                service=p["service"],
                via=list(p["via"]),
                past_incidents=int(p["past_incidents"]),
            )
            for p in result["peers"]
        ],
    )


@app.get("/incidents/{incident_id}", response_model=IncidentOut)
def get_one_incident(incident_id: str):
    incident = get_incident(require_graph(), incident_id)
    if incident is None:
        raise HTTPException(status_code=404, detail=f"no incident {incident_id}")
    if incident.get("time_to_resolve_min") is not None:
        incident["time_to_resolve_min"] = int(incident["time_to_resolve_min"])
    return IncidentOut(**incident)


@app.get("/services")
def list_services():
    """Every service and what it depends on — the shape the at-risk answer
    comes from, exposed so a UI can show the dependency graph."""
    result = require_graph().ro_query(
        """
        MATCH (s:Service)
        OPTIONAL MATCH (s)-[:DEPENDS_ON]->(d:Dependency)
        OPTIONAL MATCH (s)<-[:AFFECTED]-(i:Incident)
        RETURN s.name AS service, s.team AS team, s.language AS language,
               collect(DISTINCT d.name) AS dependencies,
               count(DISTINCT i) AS incidents
        ORDER BY service
        """
    )
    return [
        {
            "service": row[0],
            "team": row[1],
            "language": row[2],
            "dependencies": sorted(row[3] or []),
            "incidents": int(row[4]),
        }
        for row in result.result_set
    ]
