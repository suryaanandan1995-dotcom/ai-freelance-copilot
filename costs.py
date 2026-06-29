"""Claude API cost tracking + per-run budget guardrail.

Prices are USD per 1,000,000 tokens (input, output), sourced from the Claude
pricing reference (Opus 4.8: $5 / $25; Sonnet 4.6: $3 / $15). Update here if
pricing changes.
"""
from __future__ import annotations

# model id -> (input $/MTok, output $/MTok)
PRICING: dict[str, tuple[float, float]] = {
    "claude-opus-4-8": (5.0, 25.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
}

_DEFAULT_PRICE = (5.0, 25.0)  # fall back to Opus-tier if an unknown model appears


class BudgetExhausted(Exception):
    """Raised when a run's cumulative Claude spend reaches the configured cap."""


class CostTracker:
    """Accumulates token usage per model and reports cumulative USD spend.

    One tracker is created per pipeline run. The LLM wrapper records usage after
    each call and checks ``would_exceed`` before the next one.
    """

    def __init__(self, budget_usd: float | None = None) -> None:
        self.budget_usd = budget_usd
        self.input_tokens: dict[str, int] = {}
        self.output_tokens: dict[str, int] = {}
        self.calls = 0

    def record(self, model: str, input_tokens: int, output_tokens: int) -> None:
        self.input_tokens[model] = self.input_tokens.get(model, 0) + max(0, input_tokens)
        self.output_tokens[model] = self.output_tokens.get(model, 0) + max(0, output_tokens)
        self.calls += 1

    def usd(self) -> float:
        total = 0.0
        for model, in_tok in self.input_tokens.items():
            in_rate, out_rate = PRICING.get(model, _DEFAULT_PRICE)
            total += in_tok / 1_000_000 * in_rate
            total += self.output_tokens.get(model, 0) / 1_000_000 * out_rate
        return round(total, 6)

    def would_exceed(self) -> bool:
        """True if spend has already reached the budget (used as a pre-call gate)."""
        return self.budget_usd is not None and self.usd() >= self.budget_usd

    def check(self) -> None:
        if self.would_exceed():
            raise BudgetExhausted(
                f"Claude spend ${self.usd():.4f} reached the ${self.budget_usd:.2f} per-run cap"
            )

    def summary(self) -> dict:
        return {
            "calls": self.calls,
            "input_tokens": sum(self.input_tokens.values()),
            "output_tokens": sum(self.output_tokens.values()),
            "usd": self.usd(),
        }
