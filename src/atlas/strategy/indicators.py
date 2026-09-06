"""Fixed indicator library (STRAT-07).

The LLM composes from these primitives; it cannot introduce new ones. Every function is
pure, operates on Decimal, and returns a list aligned to the input with `None` where the
indicator has insufficient history.

Alignment matters: `result[i]` is the indicator value *as of the close of bar i*, using
only bars <= i. Nothing here may read forward (BT-01).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from decimal import Decimal

from atlas.data.models import Kline

Series = list[Decimal | None]
CloseIndicator = Callable[[Sequence[Decimal], int], Series]
BarIndicator = Callable[[Sequence[Kline], int], Series]


def _closes(bars: Sequence[Kline]) -> list[Decimal]:
    return [bar.close for bar in bars]


def sma(values: Sequence[Decimal], period: int) -> Series:
    """Simple moving average."""
    if period < 1:
        raise ValueError("period must be >= 1")
    out: Series = [None] * len(values)
    running = Decimal(0)
    for i, value in enumerate(values):
        running += value
        if i >= period:
            running -= values[i - period]
        if i >= period - 1:
            out[i] = running / period
    return out


def ema(values: Sequence[Decimal], period: int) -> Series:
    """Exponential moving average, seeded with the SMA of the first `period` values."""
    if period < 1:
        raise ValueError("period must be >= 1")
    out: Series = [None] * len(values)
    if len(values) < period:
        return out
    multiplier = Decimal(2) / (Decimal(period) + 1)
    seed = sum(values[:period], Decimal(0)) / period
    out[period - 1] = seed
    previous = seed
    for i in range(period, len(values)):
        previous = (values[i] - previous) * multiplier + previous
        out[i] = previous
    return out


def rsi(values: Sequence[Decimal], period: int = 14) -> Series:
    """Wilder's RSI.

    Returns 100 when average loss is zero: an unbroken run of gains is maximally
    overbought, and dividing by zero is not the right way to say so.
    """
    if period < 1:
        raise ValueError("period must be >= 1")
    out: Series = [None] * len(values)
    if len(values) <= period:
        return out

    gains: list[Decimal] = []
    losses: list[Decimal] = []
    for i in range(1, len(values)):
        change = values[i] - values[i - 1]
        gains.append(max(change, Decimal(0)))
        losses.append(max(-change, Decimal(0)))

    avg_gain = sum(gains[:period], Decimal(0)) / period
    avg_loss = sum(losses[:period], Decimal(0)) / period
    out[period] = Decimal(100) if avg_loss == 0 else _rsi_from(avg_gain, avg_loss)

    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        out[i + 1] = Decimal(100) if avg_loss == 0 else _rsi_from(avg_gain, avg_loss)
    return out


def _rsi_from(avg_gain: Decimal, avg_loss: Decimal) -> Decimal:
    rs = avg_gain / avg_loss
    return Decimal(100) - (Decimal(100) / (Decimal(1) + rs))


def true_range(bars: Sequence[Kline]) -> Series:
    """True range: max of (high-low), |high-prev_close|, |low-prev_close|."""
    out: Series = [None] * len(bars)
    for i, bar in enumerate(bars):
        if i == 0:
            out[i] = bar.high - bar.low
            continue
        prev_close = bars[i - 1].close
        out[i] = max(
            bar.high - bar.low,
            abs(bar.high - prev_close),
            abs(bar.low - prev_close),
        )
    return out


def atr(bars: Sequence[Kline], period: int = 14) -> Series:
    """Average true range, Wilder-smoothed."""
    if period < 1:
        raise ValueError("period must be >= 1")
    ranges = true_range(bars)
    out: Series = [None] * len(bars)
    if len(bars) < period:
        return out
    window = [r for r in ranges[:period] if r is not None]
    previous = sum(window, Decimal(0)) / period
    out[period - 1] = previous
    for i in range(period, len(bars)):
        current = ranges[i]
        if current is None:
            continue
        previous = (previous * (period - 1) + current) / period
        out[i] = previous
    return out


def rolling_high(bars: Sequence[Kline], period: int) -> Series:
    """Highest high over the trailing `period` bars, inclusive of the current bar."""
    if period < 1:
        raise ValueError("period must be >= 1")
    out: Series = [None] * len(bars)
    for i in range(len(bars)):
        if i >= period - 1:
            out[i] = max(bar.high for bar in bars[i - period + 1 : i + 1])
    return out


def rolling_low(bars: Sequence[Kline], period: int) -> Series:
    if period < 1:
        raise ValueError("period must be >= 1")
    out: Series = [None] * len(bars)
    for i in range(len(bars)):
        if i >= period - 1:
            out[i] = min(bar.low for bar in bars[i - period + 1 : i + 1])
    return out


def volume_sma(bars: Sequence[Kline], period: int) -> Series:
    return sma([bar.volume for bar in bars], period)


# Registry consumed by the spec schema. A spec may only name a key present here
# (STRAT-07): the LLM composes from this set and cannot introduce a primitive.
BAR_INDICATORS: dict[str, BarIndicator] = {
    "atr": atr,
    "rolling_high": rolling_high,
    "rolling_low": rolling_low,
    "volume_sma": volume_sma,
    # period is unused for true range, but the registry signature is uniform.
    "true_range": lambda bars, _period: true_range(bars),
}

CLOSE_INDICATORS: dict[str, CloseIndicator] = {
    "sma": sma,
    "ema": ema,
    "rsi": rsi,
}

INDICATOR_NAMES = frozenset(BAR_INDICATORS) | frozenset(CLOSE_INDICATORS)


def compute(name: str, bars: Sequence[Kline], period: int) -> Series:
    """Compute a named indicator. Unknown names raise (STRAT-07)."""
    if name in CLOSE_INDICATORS:
        return CLOSE_INDICATORS[name](_closes(bars), period)
    if name in BAR_INDICATORS:
        return BAR_INDICATORS[name](bars, period)
    raise ValueError(f"unknown indicator {name!r}; permitted: {sorted(INDICATOR_NAMES)}")
