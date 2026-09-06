"""Selection gates (SEL-01..07).

David names every criterion in this section and supplies no number for any of them
[11:41-11:54, 23:00-23:42]. The criteria are his; **every threshold here is an ATLAS
decision** and lives in configuration so it can be recalibrated once real throughput
data exists.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from atlas.backtest.stats import BacktestStats


@dataclass(frozen=True)
class SelectionThresholds:
    """ATLAS decisions. Not attributable to the source video."""

    min_trades: int = 100
    max_drawdown: Decimal = Decimal("0.25")
    min_profit_factor: Decimal = Decimal("1.30")
    min_expectancy: Decimal = Decimal(0)
    require_beats_buy_and_hold: bool = True
    min_oos_profit_factor: Decimal = Decimal("1.10")
    min_oos_retention: Decimal = Decimal("0.70")
    stop_band_low: Decimal = Decimal("0.03")
    stop_band_high: Decimal = Decimal("0.20")


@dataclass(frozen=True)
class GateResult:
    gate: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class SelectionOutcome:
    passed: bool
    gates: list[GateResult]

    @property
    def failures(self) -> list[GateResult]:
        return [g for g in self.gates if not g.passed]

    def as_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "gates": [{"gate": g.gate, "passed": g.passed, "detail": g.detail} for g in self.gates],
        }


def evaluate_gates(
    in_sample: BacktestStats,
    *,
    out_of_sample: BacktestStats | None = None,
    median_stop_distance: Decimal | None = None,
    thresholds: SelectionThresholds | None = None,
) -> SelectionOutcome:
    """Apply every selection gate. All gates are evaluated, not short-circuited.

    Reporting every failure at once makes the research loop's rejection reasons
    analysable in aggregate; stopping at the first would hide the distribution.
    """
    t = thresholds or SelectionThresholds()
    gates: list[GateResult] = []

    gates.append(
        GateResult(
            "SEL-01 trade_count",
            in_sample.trade_count >= t.min_trades,
            f"{in_sample.trade_count} trades (need >= {t.min_trades})",
        )
    )
    gates.append(
        GateResult(
            "SEL-02 max_drawdown",
            in_sample.max_drawdown <= t.max_drawdown,
            f"{in_sample.max_drawdown:.2%} (limit {t.max_drawdown:.0%})",
        )
    )
    gates.append(
        GateResult(
            "SEL-03 profit_factor",
            in_sample.profit_factor >= t.min_profit_factor,
            f"{in_sample.profit_factor} (need >= {t.min_profit_factor})",
        )
    )
    gates.append(
        GateResult(
            "SEL-04 expectancy",
            in_sample.expectancy > t.min_expectancy,
            f"{in_sample.expectancy} per trade (need > {t.min_expectancy})",
        )
    )
    if t.require_beats_buy_and_hold:
        gates.append(
            GateResult(
                "SEL-05 beats_buy_and_hold",
                in_sample.beats_buy_and_hold,
                f"strategy {in_sample.net_return:.2%} vs hold {in_sample.buy_and_hold_return:.2%}",
            )
        )

    if out_of_sample is not None:
        retention_ok = (
            in_sample.profit_factor <= 0
            or out_of_sample.profit_factor >= in_sample.profit_factor * t.min_oos_retention
        )
        gates.append(
            GateResult(
                "SEL-06 out_of_sample",
                out_of_sample.profit_factor >= t.min_oos_profit_factor and retention_ok,
                f"OOS PF {out_of_sample.profit_factor} vs IS {in_sample.profit_factor} "
                f"(need >= {t.min_oos_profit_factor} and >= {t.min_oos_retention} x IS)",
            )
        )

    if median_stop_distance is not None:
        within = t.stop_band_low <= median_stop_distance <= t.stop_band_high
        gates.append(
            GateResult(
                "SEL-07 stop_distance_feasible",
                within,
                f"median stop {median_stop_distance:.2%} vs feasible band "
                f"{t.stop_band_low:.0%}-{t.stop_band_high:.0%}",
            )
        )

    return SelectionOutcome(passed=all(g.passed for g in gates), gates=gates)
