"""Triage a live alert against the incident history.

    python scripts/triage.py --service refund-service ^
        --error "HikariPool-1 - Connection is not available, request timed out after 30000ms." ^
        --tags connection-pool,timeout,database

Add --explainer claude to have Claude write the briefing (needs
ANTHROPIC_API_KEY in .env). The default template explainer needs no key.
"""

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dejavu.agent import (  # noqa: E402
    ClaudeExplainer,
    OpenAIExplainer,
    TemplateExplainer,
    triage,
)
from dejavu.retrieve import Alert  # noqa: E402
from run_cypher import connect, load_env  # noqa: E402


def parse_args():
    p = argparse.ArgumentParser(description="Triage an alert against past incidents.")
    p.add_argument("--service", required=True)
    p.add_argument("--error", required=True)
    p.add_argument("--tags", default="")
    p.add_argument("--limit", type=int, default=5)
    p.add_argument(
        "--explainer",
        choices=["template", "openai", "claude"],
        default="template",
        help="template needs no API key; openai and claude write a better briefing",
    )
    p.add_argument(
        "--model",
        help="override the model id for the openai or claude explainer",
    )
    p.add_argument("--show-evidence", action="store_true",
                   help="print the exact text the explainer was given")
    return p.parse_args()


EXPLAINERS = {
    # choice: (class, env var holding the key, where to get one, pip package)
    "openai": (OpenAIExplainer, "OPENAI_API_KEY",
               "https://platform.openai.com/api-keys", "openai"),
    "claude": (ClaudeExplainer, "ANTHROPIC_API_KEY",
               "https://console.anthropic.com", "anthropic"),
}


def build_explainer(choice: str, model: str | None):
    if choice == "template":
        return TemplateExplainer()

    cls, env_var, console_url, package = EXPLAINERS[choice]
    if not os.environ.get(env_var):
        sys.exit(
            f"--explainer {choice} needs an API key.\n"
            f"Add {env_var}=... to your .env (get one at {console_url}), "
            f"or drop the flag to use the template explainer."
        )
    try:
        return cls(model=model) if model else cls()
    except ImportError:
        sys.exit(f"The {package} package is not installed. Run:\n"
                 f"  .venv\\Scripts\\pip.exe install {package}")


def main():
    args = parse_args()
    load_env()  # must run before build_explainer, which reads ANTHROPIC_API_KEY

    alert = Alert(
        service=args.service,
        error=args.error,
        tags=[t.strip() for t in args.tags.split(",") if t.strip()],
    )
    explainer = build_explainer(args.explainer, args.model)
    result = triage(connect(), alert, explainer, limit=args.limit)

    if args.show_evidence:
        from dejavu.agent import build_evidence

        print("\n--- evidence given to the explainer ---")
        print(build_evidence(alert, result["matches"], result["peers"]))
        print("--- end evidence ---")

    # ASCII on purpose: Windows consoles default to a non-UTF-8 code page and
    # render accented characters as mojibake.
    print(f"\n=== Deja Vu: {alert.service} ===\n")
    print(result["summary"])
    print()
    if result["matches"]:
        ids = ", ".join(m.id for m in result["matches"])
        print(f"(drawn from {ids} - check the briefing against them)")


if __name__ == "__main__":
    main()
