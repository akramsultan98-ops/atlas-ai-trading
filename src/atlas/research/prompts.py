"""Generation prompt (Phase 7).

David reports that longer prompts produced worse results: "the more information you give
it actually kind of reduces the results that you get" [07:36]. The prompt below is
deliberately short.

Two instructions come from the video and are marked as such; the rest is ATLAS framing
required by our schema. His own "boilerplate" — years of accumulated base rules
[07:37-07:54] — is not disclosed anywhere in the source and is not reconstructed here.
"""

from __future__ import annotations

SYSTEM_PROMPT = """You design systematic trading strategies as structured data.

You are a research component. You do not trade, size positions, or manage risk — a
separate deterministic engine does all of that and will reject anything unsound.

Rules:
- Research varied trading concepts and indicators. Do not converge on the same
  well-known setups; vary the family of idea between proposals.
- Every strategy must define its entry conditions, a stop loss and a take profit,
  all computable at the moment of entry.
- Trailing stops do not exist in this system and cannot be expressed.
- Prefer ideas whose stop distance sits between 3% and 20% of entry price. Outside
  that band the position cannot be sized on a small account and will be discarded.
- Compose only from the indicators available to you. You cannot invent new ones.
"""

USER_PROMPT_TEMPLATE = """Design one trading strategy specification.

Symbol: {symbol}
Timeframe: {timeframe}
Available indicators: {indicators}

{variation_hint}

Return the specification as structured data."""

VARIATION_HINTS = (
    "Explore a trend-following idea.",
    "Explore a mean-reversion idea.",
    "Explore a breakout idea.",
    "Explore a volatility-expansion idea.",
    "Explore a momentum-continuation idea.",
    "Explore a range-rejection idea.",
)


def build_user_prompt(symbol: str, timeframe: str, indicators: list[str], variant: int) -> str:
    return USER_PROMPT_TEMPLATE.format(
        symbol=symbol,
        timeframe=timeframe,
        indicators=", ".join(sorted(indicators)),
        variation_hint=VARIATION_HINTS[variant % len(VARIATION_HINTS)],
    )
