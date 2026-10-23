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

from dataclasses import dataclass, field
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


# ---------------------------------------------------------------------------
# Advisories
# ---------------------------------------------------------------------------
#
# These are checked against an INDEPENDENT ORACLE: who should be warned is
# recomputed in plain Python from data/incidents.json, by a different method
# than the Cypher under test. Asserting the query against itself proves
# nothing; two methods agreeing is evidence.


def expected_warned(data: dict, service: str, tags: List[str]) -> set:
    """Who should be warned about this alert, computed without the graph.

    Mirrors the two rules in EXPOSED_QUERY:
      - the dependency must be common to EVERY service that has had this
        family, otherwise it is not implicated;
      - a peer that has already had this family is not warned.
    """
    from dejavu.families import markers_of

    markers = set(markers_of(tags))
    if not markers:
        return set()

    deps = {s["name"]: {d["name"] for d in s["dependencies"]} for s in data["services"]}

    had_family = {
        inc["service"] for inc in data["incidents"] if markers & set(inc["tags"])
    }
    if not had_family:
        return set()

    # Dependencies every affected service shares.
    implicated = set.intersection(*(deps[s] for s in had_family))
    if not implicated:
        return set()

    mine = deps.get(service, set()) & implicated
    return {
        peer
        for peer, peer_deps in deps.items()
        if peer != service and (peer_deps & mine) and peer not in had_family
    }


@dataclass
class AdvisoryResult:
    name: str
    service: str
    expected: set
    warned: set
    advisory_count: int
    duplicate_ids: List[str] = field(default_factory=list)

    @property
    def correct(self) -> bool:
        return self.warned == self.expected and not self.duplicate_ids


def run_advisories(graph, spec: dict = None) -> dict:
    """Raise advisories for every eval alert and check who was warned.

    Writes to the graph, so it clears only the advisories it created — never
    everything, in case the instance holds real ones.
    """
    from dejavu.advisory import list_advisories, raise_advisories
    from dejavu.loader import read_data
    from dejavu.retrieve import Alert, find_similar

    spec = spec if spec is not None else load_eval()
    data = read_data()
    results, created = [], set()

    for case in spec["alerts"] + spec.get("negatives", []):
        alert = Alert(
            service=case["service"], error=case["error"], tags=case.get("tags", [])
        )
        matches = find_similar(graph, alert, limit=5)
        raised = raise_advisories(graph, alert, matches)
        created.update(a.id for a in raised)

        ids = [a.id for a in raised]
        results.append(
            AdvisoryResult(
                name=case["name"],
                service=case["service"],
                expected=expected_warned(data, case["service"], case.get("tags", []))
                if matches
                else set(),
                warned={a.service for a in raised},
                advisory_count=len(raised),
                duplicate_ids=sorted({i for i in ids if ids.count(i) > 1}),
            )
        )

    # Idempotence, measured rather than assumed: re-raise everything and check
    # the population did not grow.
    before = len(list_advisories(graph))
    for case in spec["alerts"]:
        alert = Alert(service=case["service"], error=case["error"], tags=case["tags"])
        raise_advisories(graph, alert, find_similar(graph, alert, limit=5))
    after = len(list_advisories(graph))

    for advisory_id in created:
        graph.query("MATCH (a:Advisory {id: $id}) DETACH DELETE a", params={"id": advisory_id})

    return {
        "cases": results,
        "correct": sum(r.correct for r in results),
        "total": len(results),
        "advisories_before_rerun": before,
        "advisories_after_rerun": after,
        "idempotent": before == after,
    }


def advisory_report(results: dict) -> str:
    lines = [
        "",
        "ADVISORY EVALUATION",
        "=" * 78,
        f"{'service':<22}{'ok':<5}{'warned':<34}expected",
        "-" * 78,
    ]
    for r in results["cases"]:
        lines.append(
            f"{r.service:<22}"
            f"{'yes' if r.correct else 'NO':<5}"
            f"{(', '.join(sorted(r.warned)) or '(nobody)'):<34}"
            f"{', '.join(sorted(r.expected)) or '(nobody)'}"
        )
        if not r.correct:
            lines.append(f"{'':<22}^ {r.name}")
            if r.duplicate_ids:
                lines.append(f"{'':<22}  duplicates: {', '.join(r.duplicate_ids)}")
    lines += [
        "-" * 78,
        f"agreement    {results['correct']}/{results['total']} cases match the "
        f"independent oracle",
        f"idempotent   {'yes' if results['idempotent'] else 'NO'} "
        f"({results['advisories_before_rerun']} advisories before re-raising, "
        f"{results['advisories_after_rerun']} after)",
        "",
    ]
    return "\n".join(lines)
