"""Cross-engine comparison (VER-02, VER-03)."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from atlas.backtest.engine import BacktestResult
from atlas.verify.vector_engine import VerifierResult

TRADE_COUNT_TOLERANCE = Decimal("0.02")
NET_RETURN_TOLERANCE = Decimal("0.05")


@dataclass(frozen=True)
class VerificationOutcome:
    passed: bool
    trade_count_delta: Decimal
    net_return_delta: Decimal
    engine_defect_suspected: bool
    reasons: list[str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "trade_count_delta": str(self.trade_count_delta),
            "net_return_delta": str(self.net_return_delta),
            "engine_defect_suspected": self.engine_defect_suspected,
            "reasons": self.reasons,
        }


def _relative_delta(a: Decimal, b: Decimal) -> Decimal:
    """Relative difference, falling back to absolute when the base is ~zero."""
    base = max(abs(a), abs(b))
    if base == 0:
        return Decimal(0)
    return abs(a - b) / base


def compare(
    primary: BacktestResult,
    verifier: VerifierResult,
    *,
    trade_count_tolerance: Decimal = TRADE_COUNT_TOLERANCE,
    net_return_tolerance: Decimal = NET_RETURN_TOLERANCE,
) -> VerificationOutcome:
    """Compare two independent engines over the same spec and data.

    A mismatch rejects the candidate *and* flags a suspected engine defect. The two
    engines implement the same semantics, so disagreement means one of them is wrong —
    that is a bug in ATLAS, not a verdict on the strategy.
    """
    reasons: list[str] = []

    primary_trades = Decimal(primary.stats.trade_count)
    verifier_trades = Decimal(verifier.trade_count)
    trade_delta = _relative_delta(primary_trades, verifier_trades)
    return_delta = _relative_delta(primary.stats.net_return, verifier.net_return)

    if trade_delta > trade_count_tolerance:
        reasons.append(
            f"trade count differs by {trade_delta:.2%}: primary {primary.stats.trade_count}, "
            f"verifier {verifier.trade_count} (tolerance {trade_count_tolerance:.0%})"
        )
    if return_delta > net_return_tolerance:
        reasons.append(
            f"net return differs by {return_delta:.2%}: primary {primary.stats.net_return}, "
            f"verifier {verifier.net_return} (tolerance {net_return_tolerance:.0%})"
        )

    # Both engines finding zero trades is agreement, not a defect.
    trivial = primary.stats.trade_count == 0 and verifier.trade_count == 0

    return VerificationOutcome(
        passed=not reasons,
        trade_count_delta=trade_delta,
        net_return_delta=return_delta,
        engine_defect_suspected=bool(reasons) and not trivial,
        reasons=reasons,
    )
