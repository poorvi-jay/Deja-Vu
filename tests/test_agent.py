"""Tests for the agent loop.

The ClaudeExplainer tests use a fake client, so the request it builds is
verified without an API key and without spending money. What they cannot check
is how Claude actually writes — that needs a real key and a human reading the
output.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from dejavu.agent import (  # noqa: E402
    SYSTEM_PROMPT,
    ClaudeExplainer,
    OpenAIExplainer,
    TemplateExplainer,
    build_evidence,
    triage,
)
from dejavu.retrieve import Alert, Match  # noqa: E402


def make_match(**overrides) -> Match:
    defaults = dict(
        id="INC-001",
        title="checkout-api 504s during evening peak",
        severity="SEV2",
        occurred_at="2025-08-14T19:05:00Z",
        service="checkout-api",
        score=21,
        signature_match=True,
        shared_tags=["connection-pool", "timeout"],
        shared_dependencies=["HikariCP"],
        same_service=False,
        resolution="Raised maximumPoolSize 10 -> 30.",
        fixed_by="priya.n",
    )
    defaults.update(overrides)
    return Match(**defaults)


@pytest.fixture
def alert():
    return Alert(
        service="refund-service",
        error="HikariPool-1 - Connection is not available, request timed out after 30000ms.",
        tags=["connection-pool", "timeout"],
    )


@pytest.fixture
def peers():
    return [
        {"service": "checkout-api", "via": ["HikariCP"], "past_incidents": 2},
        {"service": "search-api", "via": ["HikariCP"], "past_incidents": 0},
    ]


# --------------------------------------------------------------------------
# Evidence
# --------------------------------------------------------------------------


def test_evidence_contains_every_retrieved_fact(alert, peers):
    text = build_evidence(alert, [make_match()], peers)
    for expected in [
        "refund-service",
        "INC-001",
        "Raised maximumPoolSize 10 -> 30.",
        "priya.n",
        "checkout-api",
        "search-api",
    ]:
        assert expected in text


def test_evidence_states_when_nothing_matched(alert, peers):
    assert "nothing in the incident history resembles this" in build_evidence(
        alert, [], peers
    )


def test_evidence_marks_untouched_peers(alert, peers):
    text = build_evidence(alert, [make_match()], peers)
    assert "exposed but not yet affected" in text


# --------------------------------------------------------------------------
# TemplateExplainer — deterministic, so assert on the words
# --------------------------------------------------------------------------


def test_template_names_the_top_match_and_its_fix(alert, peers):
    out = TemplateExplainer().explain(alert, [make_match()], peers)
    assert "INC-001" in out
    assert "Raised maximumPoolSize 10 -> 30." in out


def test_template_admits_when_nothing_matched(alert, peers):
    out = TemplateExplainer().explain(alert, [], peers)
    assert "No past incident resembles this" in out
    assert "INC-" not in out  # must not invent one


def test_template_warns_when_one_error_had_several_causes(alert, peers):
    matches = [make_match(), make_match(id="INC-004", service="order-service")]
    out = TemplateExplainer().explain(alert, matches, peers)
    assert "INC-001, INC-004" in out
    assert "more than one cause" in out


def test_template_flags_exposed_but_unaffected_peers(alert, peers):
    out = TemplateExplainer().explain(alert, [make_match()], peers)
    assert "search-api" in out          # zero past incidents
    assert "never affected" in out


# --------------------------------------------------------------------------
# ClaudeExplainer — fake client, no key, no network
# --------------------------------------------------------------------------


class FakeBlock:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class FakeResponse:
    def __init__(self, text, stop_reason="end_turn"):
        self.content = [FakeBlock(text)]
        self.stop_reason = stop_reason


class FakeMessages:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


class FakeClient:
    def __init__(self, text="Try raising the pool size, as in INC-001.", stop_reason="end_turn"):
        self.messages = FakeMessages(FakeResponse(text, stop_reason))


def test_claude_explainer_sends_the_evidence_and_system_prompt(alert, peers):
    client = FakeClient()
    out = ClaudeExplainer(client=client).explain(alert, [make_match()], peers)

    assert out == "Try raising the pool size, as in INC-001."
    call = client.messages.calls[0]
    assert call["model"] == "claude-opus-5"
    assert call["system"] == SYSTEM_PROMPT
    # The evidence, and nothing but the evidence, is what the model sees.
    assert call["messages"][0]["content"] == build_evidence(alert, [make_match()], peers)


def test_claude_explainer_handles_a_refusal(alert, peers):
    client = FakeClient(text="", stop_reason="refusal")
    out = ClaudeExplainer(client=client).explain(alert, [make_match()], peers)
    assert "declined" in out


def test_system_prompt_forbids_inventing_facts():
    assert "Use ONLY the incidents given to you" in SYSTEM_PROMPT


# --------------------------------------------------------------------------
# OpenAIExplainer — fake client, no key, no network
# --------------------------------------------------------------------------


class FakeChoice:
    def __init__(self, content):
        self.message = type("Msg", (), {"content": content})()


class FakeCompletions:
    def __init__(self, content):
        self.content = content
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return type("Resp", (), {"choices": [FakeChoice(self.content)]})()


class FakeOpenAIClient:
    def __init__(self, content="Raise the pool size first, as in INC-001."):
        self.chat = type("Chat", (), {"completions": FakeCompletions(content)})()


def test_openai_explainer_sends_the_evidence_and_system_prompt(alert, peers):
    client = FakeOpenAIClient()
    out = OpenAIExplainer(client=client).explain(alert, [make_match()], peers)

    assert out == "Raise the pool size first, as in INC-001."
    call = client.chat.completions.calls[0]
    assert call["model"] == "gpt-4o"
    assert call["messages"][0] == {"role": "system", "content": SYSTEM_PROMPT}
    assert call["messages"][1]["content"] == build_evidence(alert, [make_match()], peers)


def test_openai_explainer_accepts_a_model_override(alert, peers):
    client = FakeOpenAIClient()
    OpenAIExplainer(client=client, model="gpt-4o-mini").explain(alert, [make_match()], peers)
    assert client.chat.completions.calls[0]["model"] == "gpt-4o-mini"


def test_openai_explainer_handles_empty_content(alert, peers):
    """A filtered or empty completion returns None, not a string."""
    client = FakeOpenAIClient(content=None)
    assert OpenAIExplainer(client=client).explain(alert, [make_match()], peers) == ""


def test_both_model_explainers_get_identical_input(alert, peers):
    """The provider is a detail. Swapping it must not change what the model is
    told — same system prompt, same evidence, same facts."""
    openai_client, claude_client = FakeOpenAIClient(), FakeClient()
    OpenAIExplainer(client=openai_client).explain(alert, [make_match()], peers)
    ClaudeExplainer(client=claude_client).explain(alert, [make_match()], peers)

    openai_call = openai_client.chat.completions.calls[0]
    claude_call = claude_client.messages.calls[0]
    assert openai_call["messages"][0]["content"] == claude_call["system"]
    assert openai_call["messages"][1]["content"] == claude_call["messages"][0]["content"]


# --------------------------------------------------------------------------
# The loop, end to end against the real graph
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def graph():
    try:
        from run_cypher import connect, load_env

        load_env()
        g = connect()
        g.ro_query("RETURN 1")
        return g
    except SystemExit as exc:
        pytest.skip(f"no database configured: {exc}")
    except Exception as exc:
        pytest.skip(f"database not reachable: {exc}")


def test_triage_returns_facts_alongside_the_summary(graph, alert):
    result = triage(graph, alert, TemplateExplainer())
    assert result["matches"]
    assert result["summary"]
    assert result["alert"] is alert


def test_summary_only_cites_incidents_retrieval_returned(graph, alert):
    """The guard against a write-up that names an incident nobody found."""
    import re

    result = triage(graph, alert, TemplateExplainer())
    retrieved = {m.id for m in result["matches"]}
    cited = set(re.findall(r"INC-\d{3}", result["summary"]))
    assert cited <= retrieved
