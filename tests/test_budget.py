"""Budget cap tests.

The cap is the only thing standing between a debugging session and an overrun,
so it is tested harder than the thing it protects.
"""

import json

import pytest

from dejavu.budget import (
    Budget,
    BudgetExceeded,
    cost_usd,
    estimate_tokens,
    price_for,
)


@pytest.fixture
def budget(tmp_path):
    """A budget with its own ledger, so tests never touch the real .spend.json."""
    return Budget(limit_usd=1.25, ledger_path=tmp_path / "spend.json")


def test_cost_matches_the_published_rate():
    # gpt-4o-mini: $0.15 per 1M input, $0.60 per 1M output.
    assert cost_usd("gpt-4o-mini", 1_000_000, 0) == pytest.approx(0.15)
    assert cost_usd("gpt-4o-mini", 0, 1_000_000) == pytest.approx(0.60)


def test_unknown_model_is_costed_at_the_most_expensive_rate():
    """An unpriced model must not cost zero, or it could spend the whole
    budget while the ledger reported nothing."""
    assert price_for("some-model-released-next-year") == max(
        price_for("claude-opus-5"), price_for("gpt-4o")
    )
    assert cost_usd("some-model-released-next-year", 1_000, 1_000) > 0


def test_a_fresh_budget_has_spent_nothing(budget):
    assert budget.spent == 0.0
    assert budget.remaining == 1.25


def test_recording_accumulates_and_persists(budget):
    budget.record("gpt-4o-mini", 1000, 500)
    first = budget.spent
    assert first > 0

    budget.record("gpt-4o-mini", 1000, 500)
    assert budget.spent == pytest.approx(first * 2)

    # A new object reading the same file sees the same total: the cap survives
    # the process exiting.
    reloaded = Budget(limit_usd=1.25, ledger_path=budget.ledger_path)
    assert reloaded.spent == budget.spent


def test_check_allows_an_affordable_call(budget):
    worst_case = budget.check("gpt-4o-mini", "x" * 4000, 600)
    assert worst_case < 0.01


def test_check_refuses_once_the_cap_is_reached(budget):
    # Spend the lot: 1M output tokens of gpt-4o is $10, well past $1.25.
    budget.record("gpt-4o", 0, 1_000_000)
    with pytest.raises(BudgetExceeded) as exc:
        budget.check("gpt-4o-mini", "anything", 600)
    assert "cap is $1.25" in str(exc.value)


def test_check_refuses_a_call_that_would_cross_the_cap(budget):
    """The decision is made on the worst case, so the cap cannot be stepped
    over by a call that turns out larger than expected."""
    budget.record("gpt-4o-mini", 0, 2_000_000)  # $1.20 of $1.25
    assert budget.remaining == pytest.approx(0.05)
    # 200k characters is ~50k input tokens; on gpt-4o that is $0.125, well past
    # the $0.05 left, so the call must be refused before it is made.
    with pytest.raises(BudgetExceeded):
        budget.check("gpt-4o", "x" * 200_000, 600)


def test_a_corrupt_ledger_stops_spending_rather_than_resetting(budget):
    budget.ledger_path.write_text("{ this is not json", encoding="utf-8")
    with pytest.raises(BudgetExceeded) as exc:
        budget.spent
    assert "unreadable" in str(exc.value)


def test_limit_can_be_raised_deliberately_via_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DEJAVU_BUDGET_USD", "5.00")
    assert Budget(ledger_path=tmp_path / "s.json").limit_usd == 5.00


def test_default_limit_is_the_project_cap(tmp_path, monkeypatch):
    monkeypatch.delenv("DEJAVU_BUDGET_USD", raising=False)
    assert Budget(ledger_path=tmp_path / "s.json").limit_usd == 1.25


def test_estimate_tokens_is_never_zero():
    assert estimate_tokens("") == 1
    assert estimate_tokens("a" * 400) == 100


def test_summary_reads_sensibly(budget):
    assert "0 calls" in budget.summary()
    budget.record("gpt-4o-mini", 1000, 200)
    assert "(1 call)" in budget.summary()
    budget.record("gpt-4o-mini", 1000, 200)
    assert "(2 calls)" in budget.summary()


def test_ledger_records_the_model_and_token_counts(budget):
    budget.record("gpt-4o-mini", 1234, 567)
    entry = json.loads(budget.ledger_path.read_text(encoding="utf-8"))["entries"][0]
    assert entry["model"] == "gpt-4o-mini"
    assert entry["input_tokens"] == 1234
    assert entry["output_tokens"] == 567
    assert entry["at"].endswith("+00:00")
