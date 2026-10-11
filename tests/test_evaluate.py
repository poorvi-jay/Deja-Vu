"""Evaluation harness tests.

The metric arithmetic is tested without a database. The thresholds run against
the real graph and exist as a regression gate: change the scoring weights and
these tests tell you whether you improved retrieval or broke it.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from dejavu.evaluate import AlertResult, NegativeResult, load_eval, report, run  # noqa: E402

EXPECTED = ["INC-001", "INC-004", "INC-011"]


def result(returned):
    return AlertResult(
        name="t", service="s", expect_family="f", returned=returned, expected=EXPECTED
    )


# --------------------------------------------------------------------------
# Metric arithmetic
# --------------------------------------------------------------------------


def test_hit_at_1_only_counts_the_top_result():
    assert result(["INC-001", "INC-999"]).hit_at_1 is True
    assert result(["INC-999", "INC-001"]).hit_at_1 is False


def test_hit_at_1_on_an_empty_result_is_false():
    assert result([]).hit_at_1 is False


def test_precision_at_3_counts_only_the_top_three():
    assert result(["INC-001", "INC-004", "INC-011", "INC-999"]).precision_at_3 == 1.0
    assert result(["INC-001", "INC-999", "INC-998"]).precision_at_3 == pytest.approx(1 / 3)


def test_precision_at_3_divides_by_what_was_returned():
    """Two correct out of two returned is 100%, not 67%. Returning fewer
    results is not the same as returning wrong ones."""
    assert result(["INC-001", "INC-004"]).precision_at_3 == 1.0


def test_reciprocal_rank_rewards_an_early_hit():
    assert result(["INC-001"]).reciprocal_rank == 1.0
    assert result(["INC-999", "INC-001"]).reciprocal_rank == 0.5
    assert result(["INC-999", "INC-998", "INC-001"]).reciprocal_rank == pytest.approx(1 / 3)


def test_reciprocal_rank_is_zero_when_nothing_is_correct():
    assert result(["INC-999"]).reciprocal_rank == 0.0


def test_a_negative_is_correct_only_when_it_returns_nothing():
    assert NegativeResult(name="n", returned=[]).correct is True
    assert NegativeResult(name="n", returned=["INC-001"]).correct is False


def test_every_eval_family_refers_to_real_incidents():
    """A typo in a family list would silently score every alert as a miss."""
    from dejavu.loader import read_data

    known = {i["id"] for i in read_data()["incidents"]}
    spec = load_eval()
    for family, ids in spec["families"].items():
        unknown = set(ids) - known
        assert not unknown, f"{family} refers to unknown incidents: {unknown}"


def test_every_alert_names_a_known_family():
    spec = load_eval()
    for case in spec["alerts"]:
        assert case["expect_family"] in spec["families"]


# --------------------------------------------------------------------------
# Thresholds, against the real graph
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


@pytest.fixture(scope="module")
def results(graph):
    return run(graph)


def test_top_result_is_the_right_family_for_every_alert(results):
    misses = [a.name for a in results["alerts"] if not a.hit_at_1]
    assert not misses, f"hit@1 failures: {misses}"


def test_precision_at_3_stays_high(results):
    assert results["precision_at_3"] >= 0.9


def test_mrr_stays_high(results):
    assert results["mrr"] >= 0.9


def test_an_unrecognised_alert_returns_nothing(results):
    """The guard that caught a real bug: `same service` used to qualify a
    match on its own, so a crash on payment-gateway returned its TLS
    incidents. Silence is the correct answer here."""
    failures = [(x.name, x.returned) for x in results["negatives"] if not x.correct]
    assert not failures, f"negatives that returned matches: {failures}"


def test_report_renders_the_headline_numbers(results):
    text = report(results)
    assert "hit@1" in text
    assert "precision@3" in text
    assert "NEGATIVES" in text
