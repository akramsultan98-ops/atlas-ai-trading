"""Per-symbol tick decisions: what the trading loop decided, and why.

An operator watching `entries=0` cannot tell the difference between a system that
looked at the market and declined, one that never looked because no strategy is
deployed, and one that wanted to trade and was stopped by risk. Those three have
completely different remedies, and before this module the audit trail recorded the same
thing for all of them: nothing.

Every configured symbol produces exactly one decision per tick, with a machine-readable
outcome. The loop that produces entries iterates over live strategies, so a symbol with
no strategy is invisible to it -- which is precisely the case that most needs reporting.

This module decides nothing. It is a record of decisions made elsewhere.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Any

# The regime is measured from candles by `atlas.intel.regime` (trend and volatility;
# risk-on/risk-off needs cross-asset data and reports UNAVAILABLE rather than guessing).
# It is reported for every symbol that had data. It does not gate entries: the strategy
# spec decides that, and a regime filter nobody specified would be an invented rule.
REGIME_NOT_MEASURED = "NOT_MEASURED: no market data this tick"


class DecisionOutcome(StrEnum):
    """Why this symbol did or did not produce an entry on this tick.

    Ordered by how far down the pipeline the decision was made. The distinction that
    matters most is between the three families:

      *absence*  -- NO_STRATEGY, SPEC_MISSING: nothing was evaluated, and nothing will
                    be until a strategy is deployed. Not a market judgement.
      *data*     -- NO_MARKET_DATA, DATA_INVALID, DATA_STALE: the market was not
                    observable, so no judgement was possible.
      *judgement*-- NO_SIGNAL, RISK_REJECTED, ORDER_REJECTED, ENTERED: the system
                    looked and decided.
    """

    ENTERED = "ENTERED"

    # Absence of a strategy: the pipeline never ran.
    NO_STRATEGY = "NO_STRATEGY"
    SPEC_MISSING = "SPEC_MISSING"

    # The market was not observable.
    NO_MARKET_DATA = "NO_MARKET_DATA"
    DATA_INVALID = "DATA_INVALID"
    DATA_STALE = "DATA_STALE"

    # The system looked and declined.
    NO_SIGNAL = "NO_SIGNAL"
    POSITION_ALREADY_OPEN = "POSITION_ALREADY_OPEN"
    FILTERS_UNAVAILABLE = "FILTERS_UNAVAILABLE"
    RISK_REJECTED = "RISK_REJECTED"
    ORDER_REJECTED = "ORDER_REJECTED"

    # Nothing was considered: the account is halted or cannot be valued.
    HALTED = "HALTED"
    ENTRIES_SUSPENDED = "ENTRIES_SUSPENDED"

    @property
    def is_entry(self) -> bool:
        return self is DecisionOutcome.ENTERED

    @property
    def evaluated_market(self) -> bool:
        """Did the system actually form a view on this symbol's market?"""
        return self in {
            DecisionOutcome.ENTERED,
            DecisionOutcome.NO_SIGNAL,
            DecisionOutcome.RISK_REJECTED,
            DecisionOutcome.ORDER_REJECTED,
            DecisionOutcome.FILTERS_UNAVAILABLE,
        }


@dataclass(frozen=True)
class SymbolDecision:
    """One symbol, one tick, one outcome.

    Every field is what was actually observed. A field left None means the pipeline
    stopped before reaching it, which is itself diagnostic: `bars is None` says market
    data never arrived, where `bars == 0` would say it arrived empty.
    """

    symbol: str
    outcome: DecisionOutcome
    detail: str = ""
    strategy_id: str | None = None
    bars: int | None = None
    data_valid: bool | None = None
    last_bar_close: str | None = None
    bar_age_seconds: float | None = None
    # Rule-based, not scored: the per-condition truth table from `evaluator.explain`.
    conditions: list[dict[str, Any]] = field(default_factory=list)
    regime: str = REGIME_NOT_MEASURED
    signal: bool = False
    reference_price: Decimal | None = None
    stop_price: Decimal | None = None
    target_price: Decimal | None = None
    risk_reason: str | None = None
    quantity: Decimal | None = None
    notional: Decimal | None = None

    @property
    def strategy_present(self) -> bool:
        return self.strategy_id is not None

    def as_dict(self) -> dict[str, Any]:
        """JSON-safe form for the audit log. Decimals become strings (ADR-003)."""

        def num(value: Decimal | None) -> str | None:
            return None if value is None else str(value)

        return {
            "symbol": self.symbol,
            "outcome": str(self.outcome),
            "detail": self.detail,
            "strategy_id": self.strategy_id,
            "strategy_present": self.strategy_present,
            "bars": self.bars,
            "data_valid": self.data_valid,
            "last_bar_close": self.last_bar_close,
            "bar_age_seconds": self.bar_age_seconds,
            "conditions": self.conditions,
            "regime": self.regime,
            "signal": self.signal,
            "reference_price": num(self.reference_price),
            "stop_price": num(self.stop_price),
            "target_price": num(self.target_price),
            "risk_reason": self.risk_reason,
            "quantity": num(self.quantity),
            "notional": num(self.notional),
        }

    def render(self) -> list[str]:
        """Operator-readable form. One symbol per block, most decisive fact first."""
        lines = [f"{self.symbol}: {self.outcome}"]
        if self.detail:
            lines.append(f"    reason        {self.detail}")
        lines.append(f"    strategy      {self.strategy_id or 'none deployed for this symbol'}")
        if self.bars is None:
            lines.append("    market data   none this tick (fetch failed or not attempted)")
        else:
            lines.append(f"    market data   {self.bars} bars, last close {self.last_bar_close}")
            age = "unknown" if self.bar_age_seconds is None else f"{self.bar_age_seconds:.0f}s"
            valid = {True: "passed", False: "FAILED", None: "not checked"}[self.data_valid]
            lines.append(f"    validation    {valid}, bar age {age}")
        lines.append(f"    regime        {self.regime}")

        if self.outcome in {DecisionOutcome.NO_STRATEGY, DecisionOutcome.SPEC_MISSING}:
            # Nothing downstream ran, and saying "no signal" would imply it did.
            lines.append("    entry rules   not evaluated: no usable strategy")
            lines.append("    signal        not evaluated")
            lines.append("    risk/sizing   not reached")
            return lines

        if self.conditions:
            lines.append("    entry rules   (no score: rules are boolean conditions)")
            for rule_index, rule in enumerate(self.conditions):
                verdict = "FIRES" if rule.get("passed") else "does not fire"
                lines.append(f"      rule {rule_index} {rule.get('side')}: {verdict}")
                for condition in rule.get("conditions", []):
                    mark = "PASS" if condition.get("passed") else "FAIL"
                    left = condition.get("left_value") or "n/a"
                    right = condition.get("right_value") or "n/a"
                    lines.append(
                        f"        {mark} {condition.get('left')}({left}) "
                        f"{condition.get('op')} {condition.get('right')}({right})"
                    )
        elif self.strategy_present:
            lines.append("    entry rules   not evaluated")

        if self.signal:
            lines.append(
                f"    signal        entry {self.reference_price}, stop {self.stop_price}, "
                f"target {self.target_price}"
            )
        else:
            lines.append("    signal        none on the last closed bar")

        if self.risk_reason is not None:
            lines.append(f"    risk/sizing   REJECTED: {self.risk_reason}")
        elif self.quantity is not None:
            lines.append(f"    risk/sizing   accepted qty {self.quantity} (~{self.notional})")
        else:
            lines.append("    risk/sizing   not reached")
        return lines


def render_decisions(decisions: list[SymbolDecision]) -> str:
    """The whole tick as operator output."""
    if not decisions:
        return "no symbols configured"
    blocks = ["decision for each configured symbol:"]
    for decision in decisions:
        blocks.extend(decision.render())
    return "\n".join(blocks)


__all__ = [
    "REGIME_NOT_MEASURED",
    "DecisionOutcome",
    "SymbolDecision",
    "render_decisions",
]
