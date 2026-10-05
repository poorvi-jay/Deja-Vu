"""The Déjà Vu loop: normalise, retrieve, explain.

    alert -> normalize() -> find_similar() + at_risk_services() -> explainer

Only the last step involves a model, and it is deliberately the smallest step.
The model never queries the graph and never decides what is relevant - it is
handed the facts retrieval already found and asked to write them up. Everything
that determines *which* incidents come back is the scored Cypher in
`retrieve.py`, which is inspectable, testable and identical on every run.

That split is the point. If the model picked the incidents, a wrong answer
would be unexplainable and untestable. As it stands, a wrong answer is either a
retrieval bug (reproducible, fixable, covered by tests) or a writing problem
(swap the explainer).

Two explainers implement the same interface:

  TemplateExplainer - deterministic, no API key, no network. Runs in tests and
                      in a demo with no internet.
  ClaudeExplainer   - calls Claude to write the summary an on-call engineer
                      reads at 3am.
"""

from typing import List, Protocol

from dejavu.budget import Budget
from dejavu.retrieve import Alert, Match, at_risk_services, find_similar

# The briefing is asked to stay under 200 words, so ~600 tokens is generous.
# It also bounds the worst-case cost of any single call, which is what the
# budget check is costed against.
MAX_OUTPUT_TOKENS = 600


class Explainer(Protocol):
    """Turns retrieved facts into something a human reads."""

    def explain(self, alert: Alert, matches: List[Match], peers: list) -> str: ...


def build_evidence(alert: Alert, matches: List[Match], peers: list) -> str:
    """Render the retrieved facts as text.

    This is the ONLY thing the model is given about the incident history. It
    cannot look anything up, so it cannot smuggle in a service or a fix that
    retrieval did not return - anything it names that is not in here is a
    fabrication, which makes that failure easy to spot and easy to test for.
    """
    lines = [
        f"ALERT",
        f"  service: {alert.service}",
        f"  error: {alert.error}",
        f"  normalised signature: {alert.signature}",
        f"  tags: {', '.join(alert.tags) if alert.tags else '(none given)'}",
        "",
        "SIMILAR PAST INCIDENTS (ranked by the graph, highest score first)",
    ]
    if not matches:
        lines.append("  (none - nothing in the incident history resembles this)")
    for m in matches:
        lines += [
            f"  {m.id} [score {m.score}] {m.severity} on {m.service}, {m.occurred_at[:10]}",
            f"    title: {m.title}",
            f"    matched because: {m.why()}",
            f"    resolution: {m.resolution or '(none recorded)'}",
            f"    fixed by: {m.fixed_by or '(unknown)'}",
        ]
        if m.also_shares():
            lines.append(f"    shares dependency: {m.also_shares()} (context, not a cause)")

    lines += ["", "SERVICES SHARING A DEPENDENCY WITH " + alert.service]
    if not peers:
        lines.append("  (none)")
    for p in peers:
        count = int(p["past_incidents"])
        state = (
            f"{count} past incident(s) carrying these tags"
            if count
            else "no history of this failure - exposed but not yet affected"
        )
        lines.append(f"  {p['service']} via {', '.join(p['via'])}: {state}")
    return "\n".join(lines)


class TemplateExplainer:
    """Writes the summary from a fixed template. No model, no network.

    Deterministic, so tests can assert on its exact output, and a demo works
    with no API key. It cannot do what the model can - read the resolutions and
    say what they have in common - so it points at the top match instead.
    """

    def explain(self, alert: Alert, matches: List[Match], peers: list) -> str:
        if not matches:
            return (
                f"No past incident resembles this {alert.service} alert. "
                f"Treat it as new, and write it up afterwards so the next "
                f"person gets a match."
            )

        top = matches[0]
        exact = [m for m in matches if m.signature_match]
        untouched = [p for p in peers if int(p["past_incidents"]) == 0]

        parts = [
            f"Seen before: {len(matches)} past incident(s) resemble this "
            f"{alert.service} alert.",
            "",
            f"Closest match is {top.id} ({top.severity} on {top.service}, "
            f"{top.occurred_at[:10]}) - {top.title}. Matched on {top.why()}.",
        ]
        if top.resolution:
            parts.append(f"That was fixed by: {top.resolution} ({top.fixed_by})")
        if len(exact) > 1:
            ids = ", ".join(m.id for m in exact)
            parts += [
                "",
                f"{len(exact)} incidents raised this exact error before: {ids}. "
                f"Worth reading all of them before acting - the same error has "
                f"had more than one cause.",
            ]
        if untouched:
            names = ", ".join(p["service"] for p in untouched)
            parts += [
                "",
                f"Also exposed but never affected: {names}. Same dependency, "
                f"no incident yet.",
            ]
        return "\n".join(parts)


SYSTEM_PROMPT = """You are Déjà Vu, an incident triage assistant for on-call engineers.

You will be given a live alert and the past incidents a graph database matched \
to it. Write the briefing the on-call engineer reads at 3am.

Rules:
- Use ONLY the incidents given to you. Never mention a service, incident id, \
person or fix that does not appear in the evidence. If the evidence is thin, \
say so plainly.
- Cite incident ids inline, so every claim can be checked.
- Lead with what to try first and why. The engineer wants a next action, not a \
summary of the summary.
- If several past incidents share the same error but had different causes, say \
that explicitly. Do not collapse them into one answer.
- Note services exposed through a shared dependency only if it is worth acting \
on. Do not imply the shared dependency caused anything.
- No preamble, no restating the alert. Under 200 words. Plain prose."""


class ClaudeExplainer:
    """Asks Claude to write the briefing from the retrieved evidence."""

    def __init__(self, client=None, model: str = "claude-haiku-4-5", budget: Budget = None):
        if client is None:
            import anthropic  # imported lazily so the package is optional

            client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
        self.client = client
        self.model = model
        self.budget = budget if budget is not None else Budget()

    def explain(self, alert: Alert, matches: List[Match], peers: list) -> str:
        prompt = build_evidence(alert, matches, peers)
        self.budget.check(self.model, SYSTEM_PROMPT + prompt, MAX_OUTPUT_TOKENS)

        response = self.client.messages.create(
            model=self.model,
            max_tokens=MAX_OUTPUT_TOKENS,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        usage = getattr(response, "usage", None)
        if usage is not None:
            self.budget.record(self.model, usage.input_tokens, usage.output_tokens)
        if response.stop_reason == "refusal":
            return "The model declined to answer. Falling back to the retrieved facts above."
        return "".join(b.text for b in response.content if b.type == "text")


class OpenAIExplainer:
    """Asks an OpenAI chat model to write the briefing.

    Interchangeable with ClaudeExplainer: same SYSTEM_PROMPT, same evidence,
    same return type. Only the SDK call differs. That is the whole reason
    `Explainer` is a protocol rather than a single class — the model is a
    detail, and swapping it should not touch retrieval or the loop.
    """

    # gpt-4o-mini by default, not gpt-4o. This project has a $1.25 total cap,
    # and the task — rewriting facts it has been handed into prose — is well
    # within a small model. gpt-4o costs roughly 17x more for the same call.
    def __init__(self, client=None, model: str = "gpt-4o-mini", budget: Budget = None):
        if client is None:
            from openai import OpenAI  # lazy, so the package stays optional

            client = OpenAI()  # reads OPENAI_API_KEY
        self.client = client
        self.model = model
        self.budget = budget if budget is not None else Budget()

    def explain(self, alert: Alert, matches: List[Match], peers: list) -> str:
        prompt = build_evidence(alert, matches, peers)
        # Raises BudgetExceeded before spending anything if the worst case
        # would breach the cap.
        self.budget.check(self.model, SYSTEM_PROMPT + prompt, MAX_OUTPUT_TOKENS)

        response = self.client.chat.completions.create(
            model=self.model,
            max_tokens=MAX_OUTPUT_TOKENS,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
        )
        usage = getattr(response, "usage", None)
        if usage is not None:
            # The API's own figures, not our estimate.
            self.budget.record(self.model, usage.prompt_tokens, usage.completion_tokens)
        return response.choices[0].message.content or ""


def triage(graph, alert: Alert, explainer: Explainer, limit: int = 5) -> dict:
    """Run the whole loop and return both the facts and the write-up.

    The matches are returned alongside the summary on purpose: the engineer
    should be able to check the write-up against what retrieval actually found.
    """
    matches = find_similar(graph, alert, limit=limit)
    peers = at_risk_services(graph, alert)
    return {
        "alert": alert,
        "matches": matches,
        "peers": peers,
        "summary": explainer.explain(alert, matches, peers),
    }
