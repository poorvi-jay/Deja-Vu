"""API tests.

These run the real app against the real graph through FastAPI's TestClient —
no server process, but every layer below HTTP is genuine. They skip if the
database is unreachable.
"""

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from dejavu.api import app  # noqa: E402

POOL_ALERT = {
    "service": "refund-service",
    "error": "HikariPool-1 - Connection is not available, request timed out after 30000ms.",
    "tags": ["connection-pool", "timeout", "database"],
}


@pytest.fixture(scope="module")
def client():
    # The lifespan handler runs on __enter__, which is what opens the graph
    # connection; using TestClient without the context manager would leave it
    # unconnected and every test would see a 503.
    with TestClient(app) as c:
        health = c.get("/health").json()
        if health["status"] != "ok":
            pytest.skip(f"graph not ready: {health}")
        yield c


def test_health_reports_the_loaded_incident_count(client):
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["incidents"] == 27


def test_triage_returns_matches_and_a_summary(client):
    body = client.post("/triage", json=POOL_ALERT).json()

    assert body["normalised_signature"].endswith("after Nms")
    assert body["summary"]
    assert body["matches"]
    assert "INC-001" in [m["id"] for m in body["matches"]]

    scores = [m["score"] for m in body["matches"]]
    assert scores == sorted(scores, reverse=True)


def test_triage_every_match_explains_itself(client):
    for match in client.post("/triage", json=POOL_ALERT).json()["matches"]:
        assert match["why"] and match["why"] != "no evidence"


def test_triage_reports_peers_sharing_a_dependency(client):
    at_risk = client.post("/triage", json=POOL_ALERT).json()["at_risk"]
    assert {p["service"] for p in at_risk} == {
        "checkout-api",
        "order-service",
        "inventory-service",
    }


def test_triage_respects_limit(client):
    body = client.post("/triage", json={**POOL_ALERT, "limit": 2}).json()
    assert len(body["matches"]) == 2


def test_triage_rejects_a_bad_limit(client):
    assert client.post("/triage", json={**POOL_ALERT, "limit": 0}).status_code == 422


def test_triage_rejects_an_unknown_explainer(client):
    response = client.post("/triage", json={**POOL_ALERT, "explainer": "gpt2"})
    assert response.status_code == 422


def test_triage_on_an_unrecognised_error_returns_no_matches(client):
    body = client.post(
        "/triage",
        json={"service": "refund-service", "error": "totally novel failure", "tags": []},
    ).json()
    assert body["matches"] == []
    assert "No past incident resembles this" in body["summary"]


def test_get_incident(client):
    body = client.get("/incidents/INC-001").json()
    assert body["service"] == "checkout-api"
    assert body["severity"] == "SEV2"
    assert body["time_to_resolve_min"] == 38
    assert set(body["tags"]) == {"connection-pool", "timeout", "database"}
    assert body["resolution"].startswith("Raised HikariCP")


def test_get_unknown_incident_is_404(client):
    response = client.get("/incidents/INC-999")
    assert response.status_code == 404
    assert "INC-999" in response.json()["detail"]


def test_list_services_includes_the_service_with_no_incidents(client):
    services = {s["service"]: s for s in client.get("/services").json()}
    assert len(services) == 9
    # refund-service is the demo's whole point: exposed, never bitten.
    assert services["refund-service"]["incidents"] == 0
    assert "HikariCP" in services["refund-service"]["dependencies"]
    assert services["checkout-api"]["incidents"] == 4


def test_openai_explainer_without_a_server_key_is_a_400(client, monkeypatch):
    """Asking for a model the deployment cannot run is the caller's mistake,
    not a server fault."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    response = client.post("/triage", json={**POOL_ALERT, "explainer": "openai"})
    assert response.status_code == 400
    assert "OPENAI_API_KEY" in response.json()["detail"]


def test_triage_breakdown_adds_up_to_the_score(client):
    for match in client.post("/triage", json=POOL_ALERT).json()["matches"]:
        assert sum(match["breakdown"].values()) == match["score"]


def test_list_incidents_puts_every_incident_in_one_of_six_families(client):
    incidents = client.get("/incidents").json()
    assert len(incidents) == 27
    families = {}
    for incident in incidents:
        families.setdefault(incident["family"], []).append(incident["id"])
    assert "Other" not in families
    assert len(families) == 6
    # INC-023 carries both `deploy` and `connection-pool`; the pool is what ran out.
    assert "INC-023" in families["Connection pool"]
    assert len(families["Connection pool"]) == 6


def test_list_incidents_is_newest_first(client):
    dates = [i["occurred_at"] for i in client.get("/incidents").json()]
    assert dates == sorted(dates, reverse=True)


def test_budget_reports_the_cap(client):
    body = client.get("/budget").json()
    assert body["limit_usd"] > 0
    assert "spent_usd" in body


def test_root_serves_the_ui(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "Déjà Vu" in response.text
