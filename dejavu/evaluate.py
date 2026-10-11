"""Measure retrieval quality against held-out alerts.

Answers "how do you know the ranking works?" with numbers instead of a demo.

What is measured is RETRIEVAL only — which incidents come back and in what
order. No model is called, so a run is free and repeatable, and a change in
the prose can never move the score. That separation is the point: scoring
lives in Cypher we can test, so it can be measured.

Three metrics, each answering a different question:

  hit@1        Is the top result from the right root-cause family?
               What an engineer actually sees first.
  precision@3  Of the top three, how many are the right family?
               Whether the whole shortlist is useful, not just the winner.
  MRR          1/rank of the first correct result, averaged.
               Penalises burying the right answer at position 5.

Negatives are scored separately and matter as much: an alert resembling
nothing must return nothing. A system that always answers is not retrieving,
it is guessing.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import List

from dejavu.loader import read_data
from dejavu.retrieve import Alert, find_similar

ROOT = Path(__file__).resolve().parent.parent
EVAL_FILE = ROOT / "data" / "eval_alerts.json"


@dataclass
class AlertResult:
    name: str
    service: str
    expect_family: str
    returned: List[str]
    expected: List[str]
    note: str = ""

    @property
    def hit_at_1(self) -> bool:
        return bool(self.returned) and self.returned[0] in self.expected

    @property
    def precision_at_3(self) -> float:
        top = self.returned[:3]
        if not top:
            return 0.0
        return sum(1 for i in top if i in self.expected) / len(top)

    @property
    def reciprocal_rank(self) -> float:
        for position, incident_id in enumerate(self.returned, start=1):
            if incident_id in self.expected:
                return 1 / position
        return 0.0


@dataclass
class NegativeResult:
    name: str
    returned: List[str]
    note: str = ""

    @property
    def correct(self) -> bool:
        """Correct means it returned nothing."""
        return not self.returned


def load_eval(path: Path = None) -> dict:
    return read_data(path or EVAL_FILE)


def run(graph, spec: dict = None, limit: int = 5) -> dict:
    spec = spec if spec is not None else load_eval()
    families = spec["families"]

    alerts = []
    for case in spec["alerts"]:
        alert = Alert(service=case["service"], error=case["error"], tags=case["tags"])
        matches = find_similar(graph, alert, limit=limit)
        alerts.append(
            AlertResult(
                name=case["name"],
                service=case["service"],
                expect_family=case["expect_family"],
                returned=[m.id for m in matches],
                expected=families[case["expect_family"]],
                note=case.get("note", ""),
            )
        )

    negatives = []
    for case in spec.get("negatives", []):
        alert = Alert(service=case["service"], error=case["error"], tags=case["tags"])
        matches = find_similar(graph, alert, limit=limit)
        negatives.append(
            NegativeResult(
                name=case["name"],
                returned=[m.id for m in matches],
                note=case.get("note", ""),
            )
        )

    n = len(alerts) or 1
    return {
        "alerts": alerts,
        "negatives": negatives,
        "hit_at_1": sum(a.hit_at_1 for a in alerts) / n,
        "precision_at_3": sum(a.precision_at_3 for a in alerts) / n,
        "mrr": sum(a.reciprocal_rank for a in alerts) / n,
        "negatives_correct": sum(bool(x.correct) for x in negatives),
        "negatives_total": len(negatives),
    }


def report(results: dict) -> str:
    """A plain-text report, so a run can be pasted into a README or an issue."""
    lines = [
        "",
        "RETRIEVAL EVALUATION",
        "=" * 78,
        f"{'family':<17}{'hit@1':<7}{'p@3':<7}{'1/rank':<8}top 3",
        "-" * 78,
    ]
    for a in results["alerts"]:
        top3 = ", ".join(a.returned[:3]) or "(nothing)"
        lines.append(
            f"{a.expect_family:<17}"
            f"{'yes' if a.hit_at_1 else 'NO':<7}"
            f"{a.precision_at_3:<7.2f}"
            f"{a.reciprocal_rank:<8.2f}"
            f"{top3}"
        )
        if not a.hit_at_1:
            lines.append(f"{'':<17}^ {a.name}")

    lines += [
        "-" * 78,
        f"hit@1        {results['hit_at_1']:.0%}   top result is the right family",
        f"precision@3  {results['precision_at_3']:.0%}   of the top three",
        f"MRR          {results['mrr']:.2f}  first correct result's rank",
        "",
        f"NEGATIVES    {results['negatives_correct']}/{results['negatives_total']} "
        f"returned nothing, as they should",
    ]
    for x in results["negatives"]:
        if not x.correct:
            lines.append(f"  FAILED: {x.name} -> returned {', '.join(x.returned)}")
    lines.append("")
    return "\n".join(lines)
