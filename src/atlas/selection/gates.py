"""Selection gates (SEL-01..07).

David names every criterion in this section and supplies no number for any of them
[11:41-11:54, 23:00-23:42]. The criteria are his; **every threshold here is an ATLAS
decision** and lives in configuration so it can be recalibrated once real throughput
data exists.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
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


class GateVerdict(StrEnum):
    """Three outcomes, because "could not decide" is not "passed".

    A gate whose evidence is absent used to be omitted from the result entirely, so a
    candidate with no out-of-sample window passed selection having never been tested
    out of sample. Silence read as consent. UNDEFINED_POLICY makes that state explicit
    and blocking: the candidate stops, and the reason names what is missing.
    """

    PASS = "PASS"
    FAIL = "FAIL"
    UNDEFINED_POLICY = "UNDEFINED_POLICY"


@dataclass(frozen=True)
class GateResult:
    gate: str
    verdict: GateVerdict
    detail: str

    @property
    def passed(self) -> bool:
        return self.verdict is GateVerdict.PASS

    @property
    def blocks(self) -> bool:
        """Anything that is not an outright PASS stops the candidate."""
        return self.verdict is not GateVerdict.PASS

    def as_dict(self) -> dict[str, Any]:
        return {
            "gate": self.gate,
            "verdict": str(self.verdict),
            "passed": self.passed,
            "detail": self.detail,
        }


def _verdict(condition: bool) -> GateVerdict:
    return GateVerdict.PASS if condition else GateVerdict.FAIL


@dataclass(frozen=True)
class SelectionOutcome:
    passed: bool
    gates: list[GateResult]

    @property
    def failures(self) -> list[GateResult]:
        """Every gate that blocks, including the undecidable ones."""
        return [g for g in self.gates if g.blocks]

    @property
    def undefined(self) -> list[GateResult]:
        return [g for g in self.gates if g.verdict is GateVerdict.UNDEFINED_POLICY]

    def as_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "gates": [g.as_dict() for g in self.gates],
            "undefined": [g.gate for g in self.undefined],
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
            _verdict(in_sample.trade_count >= t.min_trades),
            f"{in_sample.trade_count} trades (need >= {t.min_trades})",
        )
    )
    gates.append(
        GateResult(
            "SEL-02 max_drawdown",
            _verdict(in_sample.max_drawdown <= t.max_drawdown),
            f"{in_sample.max_drawdown:.2%} (limit {t.max_drawdown:.0%})",
        )
    )
    gates.append(
        GateResult(
            "SEL-03 profit_factor",
            _verdict(in_sample.profit_factor >= t.min_profit_factor),
            f"{in_sample.profit_factor} (need >= {t.min_profit_factor})",
        )
    )
    gates.append(
        GateResult(
            "SEL-04 expectancy",
            _verdict(in_sample.expectancy > t.min_expectancy),
            f"{in_sample.expectancy} per trade (need > {t.min_expectancy})",
        )
    )
    if t.require_beats_buy_and_hold:
        gates.append(
            GateResult(
                "SEL-05 beats_buy_and_hold",
                _verdict(in_sample.beats_buy_and_hold),
                f"strategy {in_sample.net_return:.2%} vs hold {in_sample.buy_and_hold_return:.2%}",
            )
        )

    if out_of_sample is None:
        # SEL-06 is mandatory. No out-of-sample window means the question was never
        # asked, which is not the same as the answer being yes.
        gates.append(
            GateResult(
                "SEL-06 out_of_sample",
                GateVerdict.UNDEFINED_POLICY,
                "no out-of-sample window supplied; SEL-06 cannot be evaluated and the "
                "candidate cannot proceed on in-sample evidence alone",
            )
        )
    else:
        retention_ok = (
            in_sample.profit_factor <= 0
            or out_of_sample.profit_factor >= in_sample.profit_factor * t.min_oos_retention
        )
        gates.append(
            GateResult(
                "SEL-06 out_of_sample",
                _verdict(out_of_sample.profit_factor >= t.min_oos_profit_factor and retention_ok),
                f"OOS PF {out_of_sample.profit_factor} vs IS {in_sample.profit_factor} "
                f"(need >= {t.min_oos_profit_factor} and >= {t.min_oos_retention} x IS)",
            )
        )

    if median_stop_distance is None:
        # A strategy whose stop distance cannot be measured cannot be shown to be
        # tradeable at $100 (section 6), so it must not pass on the assumption that it is.
        gates.append(
            GateResult(
                "SEL-07 stop_distance_feasible",
                GateVerdict.UNDEFINED_POLICY,
                "no signals produced a measurable stop distance; feasibility against "
                "the live band cannot be established",
            )
        )
    else:
        within = t.stop_band_low <= median_stop_distance <= t.stop_band_high
        gates.append(
            GateResult(
                "SEL-07 stop_distance_feasible",
                _verdict(within),
                f"median stop {median_stop_distance:.2%} vs feasible band "
                f"{t.stop_band_low:.0%}-{t.stop_band_high:.0%}",
            )
        )

    # Every gate must be an outright PASS. UNDEFINED_POLICY blocks exactly as FAIL does.
    return SelectionOutcome(passed=all(not g.blocks for g in gates), gates=gates)
