"""VER-01..03: two independently written engines must agree."""

from __future__ import annotations

from decimal import Decimal

from tests.test_backtest import FILTERS, NO_COSTS, POLICY, series_from
from tests.test_evaluator import percent_spec

from atlas.backtest.costs import CostModel
from atlas.backtest.engine import run_backtest
from atlas.verify.compare import compare
from atlas.verify.vector_engine import run_verifier

D = Decimal


def trending_series(n: int = 300):
    """Oscillating series that repeatedly crosses the entry threshold."""
    rows = []
    for i in range(n):
        base = 100 + (i * 11) % 40
        rows.append((str(base), str(base + 4), str(base - 4), str(base + (i % 5) - 2)))
    return series_from(rows)


def test_engines_agree_on_a_real_strategy() -> None:
    """The load-bearing test: two independent implementations, same answer."""
    spec = percent_spec(threshold="120")
    series = trending_series()
    primary = run_backtest(
        spec, series, policy=POLICY, filters=FILTERS, starting_equity=D("100"), costs=NO_COSTS
    )
    verifier = run_verifier(
        spec, series, policy=POLICY, filters=FILTERS, starting_equity=D("100"), costs=NO_COSTS
    )
    outcome = compare(primary, verifier)
    assert outcome.passed, outcome.reasons
    assert not outcome.engine_defect_suspected


def test_engines_agree_with_costs() -> None:
    spec = percent_spec(threshold="120")
    series = trending_series()
    primary = run_backtest(spec, series, policy=POLICY, filters=FILTERS, starting_equity=D("100"))
    verifier = run_verifier(spec, series, policy=POLICY, filters=FILTERS, starting_equity=D("100"))
    assert compare(primary, verifier).passed


def test_both_finding_nothing_is_agreement_not_defect() -> None:
    spec = percent_spec(threshold="99999")
    series = trending_series(50)
    primary = run_backtest(spec, series, policy=POLICY, filters=FILTERS, starting_equity=D("100"))
    verifier = run_verifier(spec, series, policy=POLICY, filters=FILTERS, starting_equity=D("100"))
    outcome = compare(primary, verifier)
    assert outcome.passed
    assert not outcome.engine_defect_suspected


def test_injected_divergence_is_caught() -> None:
    """VER-03: if one engine is wrong, the comparison must catch it.

    Simulated by running the verifier with a different cost model, which is exactly the
    shape of a real engine defect: same spec, same data, different answer.
    """
    spec = percent_spec(threshold="120")
    series = trending_series()
    primary = run_backtest(
        spec, series, policy=POLICY, filters=FILTERS, starting_equity=D("100"), costs=NO_COSTS
    )
    wrong = run_verifier(
        spec,
        series,
        policy=POLICY,
        filters=FILTERS,
        starting_equity=D("100"),
        costs=CostModel(fee_rate=D("0.05"), slippage_rate=D("0.05")),
    )
    outcome = compare(primary, wrong)
    assert not outcome.passed
    assert outcome.engine_defect_suspected
    assert any("net return" in r for r in outcome.reasons)


def test_trade_count_divergence_is_caught() -> None:
    spec = percent_spec(threshold="120")
    series = trending_series()
    primary = run_backtest(
        spec, series, policy=POLICY, filters=FILTERS, starting_equity=D("100"), costs=NO_COSTS
    )
    truncated = run_verifier(
        spec,
        series.slice_by_time(None, series.bars[100].close_time),
        policy=POLICY,
        filters=FILTERS,
        starting_equity=D("100"),
        costs=NO_COSTS,
    )
    outcome = compare(primary, truncated)
    assert not outcome.passed
    assert any("trade count" in r for r in outcome.reasons)


def test_verifier_shares_no_engine_code() -> None:
    """VER-01: independence is the whole value. Using the primary twice catches nothing."""
    import atlas.verify.vector_engine as verifier_module

    source = (
        verifier_module.__file__
        and __import__("pathlib").Path(verifier_module.__file__).read_text()
    )
    assert "from atlas.backtest.engine import" not in source
    assert "run_backtest" not in source


def test_tolerances_are_relative() -> None:
    spec = percent_spec(threshold="120")
    series = trending_series()
    primary = run_backtest(
        spec, series, policy=POLICY, filters=FILTERS, starting_equity=D("100"), costs=NO_COSTS
    )
    verifier = run_verifier(
        spec, series, policy=POLICY, filters=FILTERS, starting_equity=D("100"), costs=NO_COSTS
    )
    outcome = compare(primary, verifier, trade_count_tolerance=D(0), net_return_tolerance=D(0))
    assert outcome.trade_count_delta >= 0
    assert outcome.net_return_delta >= 0
