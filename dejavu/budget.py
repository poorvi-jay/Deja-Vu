"""A hard spending cap on model calls.

This project has a total budget of $1.25. A budget you have to remember is a
budget you will exceed at 2am while debugging, so it is enforced here instead:
every call is costed from the API's own reported token usage, written to a
ledger on disk, and refused once the cap is reached.

Two deliberate choices:

- The ledger is a file, not a counter in memory. Spend has to survive the
  process exiting, or every run would start from zero and the cap would mean
  nothing.
- An unknown model is costed at the most expensive rate in the table, not a
  guess or zero. If the estimate is wrong, it must be wrong in the direction
  that stops you early.
"""

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
LEDGER_PATH = ROOT / ".spend.json"
DEFAULT_LIMIT_USD = 1.25

# USD per 1,000,000 tokens, (input, output).
#
# HARDCODED AND WILL GO STALE. These are list prices at the time of writing;
# check them against the provider's pricing page before trusting the totals.
# Being slightly wrong is fine — the cap exists so a mistake costs cents.
PRICES = {
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
    "claude-opus-5": (5.00, 25.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-haiku-4-5": (1.00, 5.00),
}


class BudgetExceeded(RuntimeError):
    """Raised instead of making a call that would breach the cap."""


@dataclass
class Entry:
    at: str
    model: str
    input_tokens: int
    output_tokens: int
    usd: float


def price_for(model: str) -> tuple:
    """Price for a model, falling back to the most expensive entry.

    An unpriced model must never cost zero: that would let an unknown model
    spend the whole budget while the ledger reported nothing.
    """
    if model in PRICES:
        return PRICES[model]
    return max(PRICES.values(), key=lambda p: p[1])


def cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    rate_in, rate_out = price_for(model)
    return (input_tokens * rate_in + output_tokens * rate_out) / 1_000_000


def estimate_tokens(text: str) -> int:
    """Rough token count for pre-flight checks.

    Four characters per token is the usual english-text approximation. It is
    only used to decide whether a call *might* breach the cap; the ledger
    always records the real figures the API reports back.
    """
    return max(1, len(text) // 4)


class Budget:
    """The ledger plus the cap."""

    def __init__(self, limit_usd: Optional[float] = None, ledger_path: Optional[Path] = None):
        if limit_usd is None:
            limit_usd = float(os.environ.get("DEJAVU_BUDGET_USD", DEFAULT_LIMIT_USD))
        self.limit_usd = limit_usd
        self.ledger_path = Path(ledger_path) if ledger_path else LEDGER_PATH

    def entries(self) -> list:
        if not self.ledger_path.exists():
            return []
        try:
            return json.loads(self.ledger_path.read_text(encoding="utf-8"))["entries"]
        except (json.JSONDecodeError, KeyError):
            # A corrupt ledger must not read as "nothing spent" — that would
            # silently reset the cap. Treat it as unusable and stop.
            raise BudgetExceeded(
                f"spend ledger at {self.ledger_path} is unreadable; "
                f"inspect or delete it deliberately before spending more"
            )

    @property
    def spent(self) -> float:
        return round(sum(e["usd"] for e in self.entries()), 6)

    @property
    def remaining(self) -> float:
        return round(self.limit_usd - self.spent, 6)

    def check(self, model: str, prompt: str, max_output_tokens: int) -> float:
        """Refuse a call whose worst case would breach the cap.

        Costed at max_output_tokens — the most the call could possibly produce
        — so the decision is made on the worst case, not the likely one.
        """
        worst_case = cost_usd(model, estimate_tokens(prompt), max_output_tokens)
        if self.spent + worst_case > self.limit_usd:
            raise BudgetExceeded(
                f"refusing to call {model}: ${self.spent:.4f} already spent, "
                f"this call could cost up to ${worst_case:.4f}, "
                f"cap is ${self.limit_usd:.2f}. "
                f"Use the template explainer, or raise DEJAVU_BUDGET_USD deliberately."
            )
        return worst_case

    def record(self, model: str, input_tokens: int, output_tokens: int) -> float:
        """Write the actual cost of a completed call to the ledger."""
        usd = cost_usd(model, input_tokens, output_tokens)
        entry = Entry(
            at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            model=model,
            input_tokens=int(input_tokens),
            output_tokens=int(output_tokens),
            usd=round(usd, 6),
        )
        entries = self.entries() + [entry.__dict__]
        self.ledger_path.write_text(
            json.dumps({"limit_usd": self.limit_usd, "entries": entries}, indent=2),
            encoding="utf-8",
        )
        return usd

    def summary(self) -> str:
        n = len(self.entries())
        return (
            f"${self.spent:.4f} spent of ${self.limit_usd:.2f} "
            f"({n} call{'s' if n != 1 else ''}), ${self.remaining:.4f} left"
        )
