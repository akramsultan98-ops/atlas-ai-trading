"""The operator CLI surface for tick decisions.

`cli.py` had no test coverage at all. The decision output is the thing an operator
reaches for when a tick is quiet, so shipping it untested would repeat the pattern this
whole diagnostic exists to break: code that is present, plausible, and never exercised.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from atlas.audit import AuditLog
from atlas.cli import _decision_from_payload, main
from atlas.db.engine import Database
from atlas.models import AuditEventType
from atlas.runtime.decisions import DecisionOutcome, SymbolDecision

D = Decimal

RISK_ENV = {
    "ATLAS_RISK_PCT": "0.01",
    "ATLAS_MAX_CONCURRENT": "3",
    "ATLAS_MAX_POSITION_PCT": "0.3333",
    "ATLAS_MAX_DEPLOYED_PCT": "0.75",
    "ATLAS_DAILY_LOSS_LIMIT": "0.05",
    "ATLAS_MAX_ACCOUNT_DD": "0.20",
}


@pytest.fixture
def cli_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    for key, value in RISK_ENV.items():
        monkeypatch.setenv(key, value)
    data_dir = tmp_path / "var"
    monkeypatch.setenv("ATLAS_DATA_DIR", str(data_dir))
    monkeypatch.chdir(tmp_path)
    return data_dir


def _record(data_dir: Path, decision: SymbolDecision) -> None:
    """Put a real decision through the real audit log the CLI will read back."""
    data_dir.mkdir(parents=True, exist_ok=True)
    db = Database(data_dir / "atlas.db")
    try:
        AuditLog(db).append(AuditEventType.TICK_DECISION, decision.as_dict(), "trading")
    finally:
        db.close()


def _no_strategy() -> SymbolDecision:
    return SymbolDecision(
        symbol="BTCUSDT",
        outcome=DecisionOutcome.NO_STRATEGY,
        detail="no strategy with status LIVE targets this symbol",
    )


def _entered() -> SymbolDecision:
    return SymbolDecision(
        symbol="BTCUSDT",
        outcome=DecisionOutcome.ENTERED,
        detail="every rule condition held",
        strategy_id="strat-1",
        bars=500,
        data_valid=True,
        last_bar_close="2026-09-06T12:00:00+00:00",
        bar_age_seconds=42.0,
        signal=True,
        reference_price=D("30000"),
        stop_price=D("29400"),
        target_price=D("31200"),
        quantity=D("0.001"),
        notional=D("30"),
    )


def test_decisions_without_a_recorded_tick_says_so(
    cli_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Silence must not be reported as 'nothing was wrong'."""
    Database(cli_env / "atlas.db").close()

    assert main(["decisions"]) == 1
    out = capsys.readouterr().out
    assert "no tick decisions recorded" in out
    assert "atlas run --once" in out


def test_decisions_renders_the_recorded_reason(
    cli_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _record(cli_env, _no_strategy())

    assert main(["decisions"]) == 0
    out = capsys.readouterr().out
    assert "BTCUSDT: NO_STRATEGY" in out
    assert "no strategy with status LIVE targets this symbol" in out
    assert "no regime filter" in out


def test_decisions_json_is_machine_readable(
    cli_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The point of a machine-readable reason is that a machine can read it."""
    _record(cli_env, _entered())

    assert main(["decisions", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload[0]["outcome"] == "ENTERED"
    assert payload[0]["symbol"] == "BTCUSDT"
    assert payload[0]["quantity"] == "0.001"
    assert payload[0]["signal"] is True


def test_decisions_shows_the_latest_per_symbol(
    cli_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _record(cli_env, _no_strategy())
    _record(cli_env, _entered())

    assert main(["decisions"]) == 0
    out = capsys.readouterr().out
    assert "ENTERED" in out
    assert "NO_STRATEGY" not in out, "the superseded decision is not the current one"


def test_a_recorded_decision_round_trips(cli_env: Path) -> None:
    """What is rendered is what was recorded, not a re-derivation of it."""
    original = _entered()
    restored = _decision_from_payload(original.as_dict())

    assert restored.outcome is DecisionOutcome.ENTERED
    assert restored.reference_price == D("30000")
    assert restored.quantity == D("0.001")
    assert restored.bar_age_seconds == 42.0
    assert restored.as_dict() == original.as_dict()


def test_the_audit_chain_still_verifies_with_decisions_in_it(cli_env: Path) -> None:
    """A new event type must not break the hash chain (AI-07)."""
    _record(cli_env, _no_strategy())
    _record(cli_env, _entered())

    db = Database(cli_env / "atlas.db")
    try:
        assert AuditLog(db).verify_chain() >= 2
    finally:
        db.close()
