"""Measure retrieval quality against the held-out alerts.

Usage:
    python scripts/evaluate.py

No model is called, so this is free to run as often as you like.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dejavu.evaluate import advisory_report, report, run, run_advisories  # noqa: E402
from run_cypher import connect, load_env  # noqa: E402


def main():
    load_env()
    graph = connect()
    results = run(graph)
    print(report(results))

    advisories = run_advisories(graph)
    print(advisory_report(advisories))

    # Non-zero exit on a clear regression, so this can gate a commit or CI.
    if results["hit_at_1"] < 0.75 or results["negatives_correct"] < results["negatives_total"]:
        sys.exit("retrieval quality below threshold")
    if advisories["correct"] < advisories["total"] or not advisories["idempotent"]:
        sys.exit("advisory behaviour below threshold")


if __name__ == "__main__":
    main()
