"""Adversarial spec validation and the factory's operator surface.

A strategy specification is data that ATLAS interprets, never code that ATLAS runs
(STRAT-01). These tests attack that boundary directly: the generator's output is
untrusted input, and a model that has been prompt-injected, fine-tuned badly, or simply
confused will eventually emit something shaped like an exploit.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from tests.test_backtest import FILTERS, POLICY, series_from
from tests.test_evaluator import percent_spec
from tests.test_selection import stats

from atlas.audit import AuditLog
from atlas.backtest.engine import run_backtest
from atlas.cli import main
from atlas.db.engine import Database
from atlas.factory.store import FactoryStore
from atlas.incubation.tracker import IncubationTracker
from atlas.models import StrategyStatus
from atlas.selection.gates import evaluate_gates
from atlas.strategy.registry import StrategyRegistry
from atlas.strategy.spec import StrategySpec

D = Decimal

RISK_ENV = {
    "ATLAS_RISK_PCT": "0.01",
    "ATLAS_MAX_CONCURRENT": "3",
    "ATLAS_MAX_POSITION_PCT": "0.3333",
    "ATLAS_MAX_DEPLOYED_PCT": "0.75",
    "ATLAS_DAILY_LOSS_LIMIT": "0.05",
    "ATLAS_MAX_ACCOUNT_DD": "0.20",
}


# ------------------------------------------------ C. executable-code injection


def _spec_payload(**overrides: Any) -> dict[str, Any]:
    payload = json.loads(percent_spec().to_json())
    payload.update(overrides)
    return payload


@pytest.mark.parametrize(
    "hostile",
    [
        "__import__('os').system('id')",
        "eval('1+1')",
        "os.system",
        "lambda: 1",
        "'; DROP TABLE strategies; --",
        "{{7*7}}",
        "../../etc/passwd",
    ],
)
def test_hostile_indicator_names_are_rejected(hostile: str) -> None:
    """The indicator name is looked up in a fixed table, never resolved dynamically."""
    payload = _spec_payload(
        indicators=[{"id": "x", "name": hostile, "period": 14}],
    )
    with pytest.raises(ValidationError):
        StrategySpec.model_validate(payload)


@pytest.mark.parametrize(
    "hostile",
    ["__class__", "os.system", "a; import os", "__globals__", "../x", "a b", "A", "1x"],
)
def test_hostile_indicator_ids_are_rejected(hostile: str) -> None:
    """The id is constrained by pattern, so it can never name an attribute path."""
    payload = _spec_payload(indicators=[{"id": hostile, "name": "sma", "period": 14}])
    with pytest.raises(ValidationError):
        StrategySpec.model_validate(payload)


def test_an_id_that_merely_looks_dangerous_is_harmless() -> None:
    """`eval` passes the pattern, and should: an id is a dictionary key.

    The pattern exists to exclude attribute paths and separators, not to blocklist
    English words. What makes the id safe is that nothing ever resolves it -- it is
    looked up in a dict of precomputed series, never getattr'd or compiled.
    """
    spec = StrategySpec.model_validate(
        _spec_payload(indicators=[{"id": "eval", "name": "sma", "period": 2}])
    )
    assert spec.indicators[0].id == "eval"

    from atlas.strategy.evaluator import evaluate

    series = series_from([("100", "112", "98", "110"), ("110", "112", "95", "99")] * 4)
    assert isinstance(evaluate(spec, series.bars), list), "interpreted, never executed"


def test_an_unknown_field_is_rejected_rather_than_ignored() -> None:
    """A field ATLAS does not know about is a field ATLAS is not honouring."""
    with pytest.raises(ValidationError):
        StrategySpec.model_validate(_spec_payload(exec_hook="print('hi')"))


def test_a_trailing_stop_field_is_rejected() -> None:
    """EXEC-08 forbids trailing stops; the schema must not silently accept one."""
    with pytest.raises(ValidationError):
        StrategySpec.model_validate(_spec_payload(trailing_stop={"kind": "percent"}))


def test_dunder_payload_keys_are_rejected() -> None:
    """Deserialisation must not be a construction primitive."""
    for key in ("__init__", "__reduce__", "__class__", "__dict__"):
        with pytest.raises(ValidationError):
            StrategySpec.model_validate(_spec_payload(**{key: "x"}))


def test_a_spec_without_a_stop_is_rejected() -> None:
    """STRAT-03: a position with no exit rule is not a strategy."""
    payload = _spec_payload()
    del payload["stop"]
    with pytest.raises(ValidationError):
        StrategySpec.model_validate(payload)


def test_a_spec_without_a_target_is_rejected() -> None:
    payload = _spec_payload()
    del payload["target"]
    with pytest.raises(ValidationError):
        StrategySpec.model_validate(payload)


def test_an_invalid_timeframe_is_rejected() -> None:
    with pytest.raises(ValidationError):
        StrategySpec.model_validate(_spec_payload(timeframe="3s"))


def test_an_empty_symbol_is_rejected() -> None:
    """Whether the symbol *exists* is settled against exchangeInfo, not the schema."""
    with pytest.raises(ValidationError):
        StrategySpec.model_validate(_spec_payload(symbol=""))


def test_an_unsupported_comparator_is_rejected() -> None:
    payload = _spec_payload()
    payload["entries"][0]["conditions"][0]["op"] = "__eq__"
    with pytest.raises(ValidationError):
        StrategySpec.model_validate(payload)


def test_hostile_text_in_a_free_field_stays_inert_data() -> None:
    """A name is displayed and hashed. It is never a template, a query or a program."""
    hostile = "__import__('os').system('touch /tmp/atlas-pwned')"
    spec = StrategySpec.model_validate(_spec_payload(name=hostile))

    assert spec.name == hostile, "stored verbatim"
    assert not Path("/tmp/atlas-pwned").exists()
    # It survives a round trip as text, and changes only the hash.
    assert StrategySpec.from_json(spec.to_json()).name == hostile
    assert spec.content_hash() != percent_spec().content_hash()


def test_specs_are_deserialised_without_eval() -> None:
    """The loader is a JSON parser. `eval` on stored data would be arbitrary execution."""
    source = Path("src/atlas/strategy/spec.py").read_text()
    for forbidden in ("eval(", "exec(", "pickle", "__import__"):
        assert forbidden not in source, f"spec.py must not contain {forbidden}"


# --------------------------------------------------------- CLI operator surface


@pytest.fixture
def cli_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    for key, value in RISK_ENV.items():
        monkeypatch.setenv(key, value)
    data_dir = tmp_path / "var"
    monkeypatch.setenv("ATLAS_DATA_DIR", str(data_dir))
    monkeypatch.chdir(tmp_path)
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir


def _seed(data_dir: Path, *, with_evidence: bool = True) -> str:
    """A real candidate with real stored evidence. Nothing is faked."""
    db = Database(data_dir / "atlas.db")
    try:
        spec = percent_spec(threshold="105")
        strategy_id = StrategyRegistry(db).register(spec)
        if with_evidence:
            series = series_from([("100", "112", "98", "110"), ("110", "112", "95", "99")] * 20)
            result = run_backtest(
                spec, series, policy=POLICY, filters=FILTERS, starting_equity=D("100")
            )
            store = FactoryStore(db)
            store.record_backtest(strategy_id, result, series, config_hash="cfg-1")
            store.record_selection(
                strategy_id,
                evaluate_gates(stats(), out_of_sample=stats(), median_stop_distance=D("0.05")),
            )
        return strategy_id
    finally:
        db.close()


def test_candidates_lists_nothing_when_the_factory_is_empty(
    cli_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    Database(cli_env / "atlas.db").close()
    assert main(["factory", "candidates"]) == 0
    assert "no strategies recorded" in capsys.readouterr().out


def test_candidates_lists_a_registered_strategy(
    cli_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    strategy_id = _seed(cli_env)
    assert main(["factory", "candidates"]) == 0
    out = capsys.readouterr().out
    assert strategy_id in out
    assert "CANDIDATE" in out


def test_show_reports_the_spec_and_its_hash(
    cli_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    strategy_id = _seed(cli_env)
    assert main(["factory", "show", strategy_id]) == 0
    out = capsys.readouterr().out
    assert percent_spec(threshold="105").content_hash() in out
    assert "CANDIDATE" in out


def test_backtests_reports_full_provenance(
    cli_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    strategy_id = _seed(cli_env)
    assert main(["factory", "backtests", strategy_id]) == 0
    out = capsys.readouterr().out
    for field in ("window", "data_hash", "config_hash", "costs", "stats"):
        assert field in out


def test_gates_shows_each_verdict(cli_env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    strategy_id = _seed(cli_env)
    assert main(["factory", "gates", strategy_id]) == 0
    out = capsys.readouterr().out
    assert "SEL-01 trade_count" in out
    assert "PASS" in out


def test_missing_evidence_is_reported_not_invented(
    cli_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    strategy_id = _seed(cli_env, with_evidence=False)
    assert main(["factory", "backtests", strategy_id]) == 1
    assert "no backtests recorded" in capsys.readouterr().out
    assert main(["factory", "verification", strategy_id]) == 1
    assert "no verification recorded" in capsys.readouterr().out


def test_incubation_reports_zero_days_before_it_starts(
    cli_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    strategy_id = _seed(cli_env)
    assert main(["factory", "incubation", strategy_id]) == 1
    out = capsys.readouterr().out
    assert "has not been started" in out
    assert "0 days" in out


def test_incubate_refuses_without_confirmation(
    cli_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    strategy_id = _seed(cli_env)
    assert main(["factory", "incubate", strategy_id]) == 2
    assert "--confirm" in capsys.readouterr().err
    db = Database(cli_env / "atlas.db")
    try:
        assert StrategyRegistry(db).status(strategy_id) is StrategyStatus.CANDIDATE
    finally:
        db.close()


def test_incubate_refuses_a_strategy_that_has_not_been_verified(
    cli_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The pipeline order is not optional."""
    strategy_id = _seed(cli_env)
    assert main(["factory", "incubate", strategy_id, "--confirm"]) == 2
    assert "not VERIFIED" in capsys.readouterr().err


def test_incubate_starts_the_clock_once_verified(
    cli_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    strategy_id = _seed(cli_env)
    db = Database(cli_env / "atlas.db")
    try:
        StrategyRegistry(db).set_status(strategy_id, StrategyStatus.VERIFIED)
    finally:
        db.close()

    assert main(["factory", "incubate", strategy_id, "--confirm"]) == 0
    out = capsys.readouterr().out
    assert "INCUBATING" in out
    assert "zero capital" in out

    db = Database(cli_env / "atlas.db")
    try:
        assert StrategyRegistry(db).status(strategy_id) is StrategyStatus.INCUBATING
        run = IncubationTracker(db).run_for(strategy_id)
        assert run is not None and run.config_hash == "cfg-1"
        assert AuditLog(db).verify_chain() > 0
    finally:
        db.close()


def test_eligibility_reports_but_never_promotes(
    cli_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Q: reading eligibility must not be a way to become LIVE."""
    strategy_id = _seed(cli_env)
    db = Database(cli_env / "atlas.db")
    try:
        StrategyRegistry(db).set_status(strategy_id, StrategyStatus.VERIFIED)
    finally:
        db.close()
    main(["factory", "incubate", strategy_id, "--confirm"])
    capsys.readouterr()

    assert main(["factory", "eligibility", strategy_id]) == 0
    out = capsys.readouterr().out
    assert "gates_satisfied     False" in out
    assert "explicit human approval" in out

    db = Database(cli_env / "atlas.db")
    try:
        assert StrategyRegistry(db).status(strategy_id) is StrategyStatus.INCUBATING
    finally:
        db.close()


def test_an_unknown_strategy_id_is_an_error_not_a_blank_report(
    cli_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    Database(cli_env / "atlas.db").close()
    assert main(["factory", "show", "does-not-exist"]) == 2
    assert "no strategy" in capsys.readouterr().err
