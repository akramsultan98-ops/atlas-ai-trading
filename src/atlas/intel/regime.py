"""Deterministic market regime from candles alone (INTEL-03).

Only states the available data can actually support. Trend and volatility are
measurable from OHLCV; risk-on/risk-off is a cross-asset judgement that BTCUSDT candles
cannot supply, so it is reported as UNAVAILABLE rather than guessed at from price
direction. A regime label nothing measured is worse than no label, because downstream
code cannot tell the difference.

No model is involved. Same candles, same regime, always.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from itertools import pairwise
from typing import Any

from atlas.data.models import Kline

ZERO = Decimal(0)


class TrendState(StrEnum):
    TRENDING_UP = "TRENDING_UP"
    TRENDING_DOWN = "TRENDING_DOWN"
    RANGING = "RANGING"
    UNCLEAR = "UNCLEAR"


class VolatilityState(StrEnum):
    HIGH = "HIGH"
    NORMAL = "NORMAL"
    LOW = "LOW"
    UNCLEAR = "UNCLEAR"


class RiskState(StrEnum):
    """Requires cross-asset data. UNAVAILABLE is the honest answer without it."""

    RISK_ON = "RISK_ON"
    RISK_OFF = "RISK_OFF"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True)
class RegimeThresholds:
    """ATLAS decisions, stated so they can be recalibrated rather than rediscovered."""

    lookback: int = 100
    trend_window: int = 20
    # Net directional travel as a fraction of total path length. A market that moves
    # 5% while travelling 50% is ranging, however far it ended from where it began.
    trend_efficiency: Decimal = Decimal("0.30")
    high_volatility_ratio: Decimal = Decimal("1.50")
    low_volatility_ratio: Decimal = Decimal("0.60")
    min_bars: int = 30


@dataclass(frozen=True)
class MarketRegime:
    trend: TrendState
    volatility: VolatilityState
    risk: RiskState
    efficiency: Decimal | None
    volatility_ratio: Decimal | None
    bars_used: int

    @property
    def is_measurable(self) -> bool:
        return self.trend is not TrendState.UNCLEAR and self.volatility is not (
            VolatilityState.UNCLEAR
        )

    @property
    def label(self) -> str:
        return f"{self.trend}/{self.volatility}/{self.risk}"

    def favours(self, long: bool) -> bool:
        """Whether the trend agrees with a direction. Ranging favours neither."""
        if self.trend is TrendState.TRENDING_UP:
            return long
        if self.trend is TrendState.TRENDING_DOWN:
            return not long
        return False

    def as_dict(self) -> dict[str, Any]:
        return {
            "trend": str(self.trend),
            "volatility": str(self.volatility),
            "risk": str(self.risk),
            "efficiency": None if self.efficiency is None else str(self.efficiency),
            "volatility_ratio": (
                None if self.volatility_ratio is None else str(self.volatility_ratio)
            ),
            "bars_used": self.bars_used,
            "measurable": self.is_measurable,
        }


UNMEASURABLE = MarketRegime(
    trend=TrendState.UNCLEAR,
    volatility=VolatilityState.UNCLEAR,
    risk=RiskState.UNAVAILABLE,
    efficiency=None,
    volatility_ratio=None,
    bars_used=0,
)


def _mean_true_range(bars: tuple[Kline, ...]) -> Decimal:
    if not bars:
        return ZERO
    total = sum((b.high - b.low for b in bars), ZERO)
    return total / len(bars)


def classify_regime(
    bars: tuple[Kline, ...] | list[Kline],
    thresholds: RegimeThresholds | None = None,
) -> MarketRegime:
    """Classify the regime from the most recent bars. Deterministic, no model.

    Trend uses directional efficiency - net movement divided by the distance actually
    travelled - rather than the sign of a return, because a market that ends where it
    started after a large round trip is ranging, not flat-trending.
    """
    t = thresholds or RegimeThresholds()
    window = tuple(bars)[-t.lookback :]
    if len(window) < t.min_bars:
        # Not enough data to measure is not "calm". Say so.
        return MarketRegime(
            trend=TrendState.UNCLEAR,
            volatility=VolatilityState.UNCLEAR,
            risk=RiskState.UNAVAILABLE,
            efficiency=None,
            volatility_ratio=None,
            bars_used=len(window),
        )

    recent = window[-t.trend_window :]
    net = recent[-1].close - recent[0].close
    path = sum((abs(b.close - a.close) for a, b in pairwise(recent)), ZERO)
    efficiency = ZERO if path <= 0 else abs(net) / path

    if efficiency >= t.trend_efficiency and net != 0:
        trend = TrendState.TRENDING_UP if net > 0 else TrendState.TRENDING_DOWN
    else:
        trend = TrendState.RANGING

    baseline = _mean_true_range(window)
    current = _mean_true_range(recent)
    if baseline <= 0:
        volatility = VolatilityState.UNCLEAR
        ratio: Decimal | None = None
    else:
        ratio = current / baseline
        if ratio >= t.high_volatility_ratio:
            volatility = VolatilityState.HIGH
        elif ratio <= t.low_volatility_ratio:
            volatility = VolatilityState.LOW
        else:
            volatility = VolatilityState.NORMAL

    return MarketRegime(
        trend=trend,
        volatility=volatility,
        # Deliberately not inferred from a single symbol's direction: risk-on/risk-off
        # is a statement about capital rotating between asset classes, and one crypto
        # pair cannot observe that.
        risk=RiskState.UNAVAILABLE,
        efficiency=efficiency,
        volatility_ratio=ratio,
        bars_used=len(window),
    )


__all__ = [
    "UNMEASURABLE",
    "MarketRegime",
    "RegimeThresholds",
    "RiskState",
    "TrendState",
    "VolatilityState",
    "classify_regime",
]
