"""Indicator library: values against known references, and no forward reads."""

from __future__ import annotations

from decimal import Decimal

import pytest
from tests.test_data_models import make_bar

from atlas.strategy import indicators as ind

D = Decimal


def bars_from_closes(closes: list[str]) -> list:
    out = []
    for i, c in enumerate(closes):
        price = D(c)
        out.append(make_bar(i, open=price, high=price + 1, low=price - 1, close=price))
    return out


def test_sma_known_values() -> None:
    values = [D(x) for x in (1, 2, 3, 4, 5, 6)]
    result = ind.sma(values, 3)
    assert result[:2] == [None, None]
    assert result[2] == D(2)  # (1+2+3)/3
    assert result[5] == D(5)  # (4+5+6)/3


def test_sma_period_one_is_identity() -> None:
    values = [D(x) for x in (7, 8, 9)]
    assert ind.sma(values, 1) == values


def test_sma_insufficient_history_is_all_none() -> None:
    assert ind.sma([D(1), D(2)], 5) == [None, None]


def test_ema_seeds_with_sma_then_smooths() -> None:
    values = [D(x) for x in (1, 2, 3, 4, 5)]
    result = ind.ema(values, 3)
    assert result[:2] == [None, None]
    assert result[2] == D(2)  # seed = SMA(1,2,3)
    # multiplier = 2/(3+1) = 0.5 -> (4 - 2) * 0.5 + 2 = 3
    assert result[3] == D(3)
    # (5 - 3) * 0.5 + 3 = 4
    assert result[4] == D(4)


def test_rsi_all_gains_is_one_hundred() -> None:
    """An unbroken run of gains is maximally overbought, not a division by zero."""
    values = [D(x) for x in range(1, 20)]
    result = ind.rsi(values, 14)
    assert result[14] == D(100)


def test_rsi_is_bounded() -> None:
    closes = [
        "10",
        "11",
        "10.5",
        "12",
        "11.5",
        "13",
        "12.5",
        "14",
        "13.5",
        "15",
        "14.5",
        "16",
        "15.5",
        "17",
        "16.5",
        "18",
        "17.5",
    ]
    result = ind.rsi([D(c) for c in closes], 14)
    for value in result:
        if value is not None:
            assert D(0) <= value <= D(100)


def test_rsi_insufficient_history() -> None:
    assert all(v is None for v in ind.rsi([D(1), D(2), D(3)], 14))


def test_true_range_first_bar_is_high_low() -> None:
    bars = bars_from_closes(["10", "11"])
    result = ind.true_range(bars)
    assert result[0] == D(2)  # high 11 - low 9


def test_true_range_accounts_for_gap() -> None:
    b0 = make_bar(0, open=D(10), high=D(11), low=D(9), close=D(10))
    b1 = make_bar(1, open=D(20), high=D(21), low=D(19), close=D(20))
    result = ind.true_range([b0, b1])
    assert result[1] == D(11)  # |21 - 10| beats the 2-wide bar range


def test_atr_smooths_true_range() -> None:
    bars = bars_from_closes([str(10 + i) for i in range(20)])
    result = ind.atr(bars, 14)
    assert result[12] is None
    assert result[13] is not None
    assert result[13] > 0


def test_rolling_high_and_low() -> None:
    bars = [make_bar(i, open=D(10), high=D(10 + i), low=D(10 - i), close=D(10)) for i in range(5)]
    highs = ind.rolling_high(bars, 3)
    lows = ind.rolling_low(bars, 3)
    assert highs[2] == D(12)
    assert highs[4] == D(14)
    assert lows[4] == D(6)


def test_indicators_never_read_forward() -> None:
    """BT-01: truncating the input must not change earlier values."""
    bars = bars_from_closes([str(100 + (i * 7) % 13) for i in range(60)])
    for name, period in (
        ("sma", 10),
        ("ema", 10),
        ("rsi", 14),
        ("atr", 14),
        ("rolling_high", 5),
        ("rolling_low", 5),
        ("volume_sma", 10),
    ):
        full = ind.compute(name, bars, period)
        truncated = ind.compute(name, bars[:40], period)
        assert full[:40] == truncated, f"{name} reads forward"


def test_unknown_indicator_rejected() -> None:
    """STRAT-07: the LLM composes from a fixed set; it cannot introduce primitives."""
    with pytest.raises(ValueError, match="unknown indicator"):
        ind.compute("secret_sauce", bars_from_closes(["10", "20"]), 2)


def test_zero_period_rejected() -> None:
    with pytest.raises(ValueError, match="period must be"):
        ind.sma([D(1)], 0)


def test_registry_names_match_compute() -> None:
    bars = bars_from_closes([str(i + 10) for i in range(40)])
    for name in ind.INDICATOR_NAMES:
        assert len(ind.compute(name, bars, 5)) == len(bars)
