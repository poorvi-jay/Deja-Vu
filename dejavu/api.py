"""HTTP API for Déjà Vu.

    uvicorn dejavu.api:app --reload

Endpoints:
    GET  /health           is the service up, and can it reach the graph
    POST /triage           an alert in, ranked incidents + a briefing out
    GET  /incidents        every incident, grouped into a root-cause family
    GET  /incidents/{id}   one incident in full
    GET  /services         every service, with its dependencies
    GET  /budget           model spend against the cap
    POST /advisories       raise advisories for peers exposed by an alert
    GET  /advisories       what is outstanding, filterable by status and team
    PATCH /advisories/{id} acknowledge, resolve or dismiss one
    GET  /                 the demo UI (static files in dejavu/static)

The route handlers are thin on purpose. All the logic lives in retrieve.py and
agent.py, which are tested without HTTP; these functions only translate between
JSON and those calls. Anything worth testing should be testable without a
server.
"""

import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Dict, List, Literal, Optional

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from dejavu.advisory import list_advisories, raise_advisories, set_status
from dejavu.agent import OpenAIExplainer, TemplateExplainer, triage
from dejavu.budget import Budget, BudgetExceeded
from dejavu.families import family_of
from dejavu.retrieve import (
    Alert,
    at_risk_services,
    find_similar,
    get_incident,
    list_incidents,
)

STATIC_DIR = Path(__file__).resolve().parent / "static"

# The connection is opened once at startup and reused. Opening one per request
# would add a round trip to every call and exhaust the connection pool under
# any real load.
state: dict = {"graph": None, "error": None, "seeded": False}

# Set DEJAVU_SEED_ON_START=0 to turn the self-seeding off.
SEED_ON_START = os.environ.get("DEJAVU_SEED_ON_START", "1") != "0"


def seed_if_empty(graph) -> bool:
    """Load the incident data when the graph has none. Returns whether it did.

    A free-tier FalkorDB instance can be recycled or recreated between demos,
    and an empty graph answers every query with nothing while looking healthy.
    Seeding is safe to attempt on every boot because the loader is built from
    MERGEs: with data already present it writes nothing.
    """
    count = graph.ro_query("MATCH (i:Incident) RETURN count(i)").result_set[0][0]
    if int(count):
        return False
    from dejavu.loader import load_all

    result = load_all(graph)
    print(
        f"[startup] graph was empty — seeded {result['incidents']} incidents, "
        f"{result['nodes_created']} nodes"
    )
    return True


@asynccontextmanager
async def lifespan(app: FastAPI):
    from run_cypher import connect, load_env

    try:
        load_env()
        state["graph"] = connect()
        state["graph"].ro_query("RETURN 1")
        if SEED_ON_START:
            state["seeded"] = seed_if_empty(state["graph"])
    except BaseException as exc:
        # Startup must not crash the process: /health should come up and
        # report the problem, which is far easier to debug on a deployed box
        # than a container that exits before it logs anything. load_env calls
        # sys.exit when configuration is missing, hence BaseException.
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
    explainer: Literal["template", "openai"] = "template"


class MatchOut(BaseModel):
    id: str
    title: str
    severity: str
    occurred_at: str
    service: str
    score: int
    why: str
    signature_match: bool
    same_service: bool
    shared_tags: List[str]
    also_shares: List[str]
    breakdown: Dict[str, int]
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


class IncidentListItem(IncidentOut):
    family: str




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
    cls, env_var = {"openai": (OpenAIExplainer, "OPENAI_API_KEY")}[choice]
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
    try:
        result = triage(graph, alert, build_explainer(request.explainer), limit=request.limit)
    except BudgetExceeded as exc:
        # 429: the request is fine, but this deployment has spent its model
        # budget. The template explainer still works, and the message says so.
        raise HTTPException(status_code=429, detail=str(exc))

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
                same_service=m.same_service,
                shared_tags=sorted(m.shared_tags),
                also_shares=sorted(m.shared_dependencies) if not m.same_service else [],
                breakdown=m.breakdown(),
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


@app.get("/incidents", response_model=List[IncidentListItem])
def get_all_incidents():
    incidents = list_incidents(require_graph())
    for incident in incidents:
        if incident.get("time_to_resolve_min") is not None:
            incident["time_to_resolve_min"] = int(incident["time_to_resolve_min"])
        incident["family"] = family_of(incident["tags"])
    return [IncidentListItem(**i) for i in incidents]


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


@app.get("/budget")
def get_budget():
    """What the model explainers have spent against the hard cap. Read from
    the same ledger the cap is enforced from, so the two cannot disagree."""
    budget = Budget()
    try:
        spent = budget.spent
    except BudgetExceeded as exc:
        return {"limit_usd": budget.limit_usd, "spent_usd": None, "detail": str(exc)}
    return {
        "limit_usd": budget.limit_usd,
        "spent_usd": spent,
        "remaining_usd": budget.remaining,
        "calls": len(budget.entries()),
    }


# ---------------------------------------------------------------------------
# Advisories
# ---------------------------------------------------------------------------


class AdvisoryOut(BaseModel):
    id: str
    status: str
    family: str
    service: str
    via: List[str]
    because_of: List[str]
    why: str
    suggested_fix: Optional[str]
    ask: Optional[str]
    routed_to: Optional[str]
    seen_count: int
    raised_at: Optional[str]
    last_seen_at: Optional[str]
    reopened_at: Optional[str]


class RaiseRequest(BaseModel):
    """The same shape as a triage request: an advisory is a side effect of
    triaging an alert, not a thing anyone composes by hand."""

    service: str = Field(..., examples=["checkout-api"])
    error: str = Field(
        ...,
        examples=["HikariPool-1 - Connection is not available, request timed out after 30000ms."],
    )
    tags: List[str] = Field(default_factory=list, examples=[["connection-pool", "timeout"]])
    limit: int = Field(5, ge=1, le=50)


class StatusUpdate(BaseModel):
    status: Literal["open", "acknowledged", "resolved", "dismissed"]


def advisory_out(advisory) -> AdvisoryOut:
    return AdvisoryOut(
        id=advisory.id,
        status=advisory.status,
        family=advisory.family,
        service=advisory.service,
        via=advisory.via,
        because_of=advisory.because_of,
        why=advisory.why(),
        suggested_fix=advisory.suggested_fix,
        ask=advisory.ask,
        routed_to=advisory.routed_to,
        seen_count=advisory.seen_count,
        raised_at=advisory.raised_at,
        last_seen_at=advisory.last_seen_at,
        reopened_at=advisory.reopened_at,
    )


@app.post("/advisories", response_model=List[AdvisoryOut])
def post_advisories(request: RaiseRequest):
    """Raise an advisory for every peer exposed by this alert.

    Safe to call repeatedly: an advisory is keyed on (service, dependency,
    family), so re-raising the same exposure updates one node rather than
    adding another. Peers that have already had this family are skipped.
    """
    graph = require_graph()
    alert = Alert(service=request.service, error=request.error, tags=request.tags)
    matches = find_similar(graph, alert, limit=request.limit)
    return [advisory_out(a) for a in raise_advisories(graph, alert, matches)]


@app.get("/advisories", response_model=List[AdvisoryOut])
def get_advisories(
    status: Optional[Literal["open", "acknowledged", "resolved", "dismissed"]] = None,
    team: Optional[str] = None,
):
    """What is outstanding. `team` answers "what is on my plate" — the whole
    reason :Team is a node."""
    return [
        advisory_out(a)
        for a in list_advisories(require_graph(), status=status, team=team)
    ]


@app.patch("/advisories/{advisory_id}", response_model=AdvisoryOut)
def patch_advisory(advisory_id: str, update: StatusUpdate):
    """Acknowledge, resolve or dismiss.

    The distinction that matters is in the graph, not here: a resolved
    advisory reopens if the family recurs, a dismissed one does not.
    """
    graph = require_graph()
    if set_status(graph, advisory_id, update.status) is None:
        raise HTTPException(status_code=404, detail=f"no advisory {advisory_id}")
    found = [a for a in list_advisories(graph) if a.id == advisory_id]
    return advisory_out(found[0])


# Mounted last, so every API route above takes precedence over a file path.
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="ui")
