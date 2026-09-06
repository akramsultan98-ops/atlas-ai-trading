from __future__ import annotations

import os
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import ClassVar

import pytest

from atlas.audit import AuditLog
from atlas.db.engine import Database
from atlas.killswitch import KillSwitch
from atlas.risk.filters import ExchangeInfoError
from atlas.risk.sizing import ExchangeFilters

_RISK_ENV = {
    "ATLAS_RISK_PCT": "0.01",
    "ATLAS_MAX_CONCURRENT": "3",
    "ATLAS_MAX_POSITION_PCT": "0.3333",
    "ATLAS_MAX_DEPLOYED_PCT": "0.75",
    "ATLAS_DAILY_LOSS_LIMIT": "0.05",
    "ATLAS_MAX_ACCOUNT_DD": "0.20",
}


@pytest.fixture
def risk_env(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """A complete, valid risk policy in the environment."""
    for key, value in _RISK_ENV.items():
        monkeypatch.setenv(key, value)
    # Ensure a stray .env in the working tree cannot influence tests.
    monkeypatch.chdir(Path(__file__).parent)
    return dict(_RISK_ENV)


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove every ATLAS_ variable so absence can be tested."""
    for key in list(os.environ):
        if key.startswith("ATLAS_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(Path(__file__).parent)


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    database = Database(tmp_path / "atlas.db")
    yield database
    database.close()


@pytest.fixture
def audit(db: Database) -> AuditLog:
    return AuditLog(db)


@pytest.fixture
def killswitch(db: Database, audit: AuditLog, tmp_path: Path) -> KillSwitch:
    return KillSwitch(db, tmp_path / "killswitch.json", audit)


class StubFilterProvider:
    """Stands in for the live exchangeInfo provider (RISK-09).

    `is_live` is True because this occupies the live provider's slot: the trading
    service refuses a provider that supplies fixed values, and a stub that lied about
    being live would let a test pass a configuration production forbids.

    `fail_for` makes one symbol's filters unobtainable, which is the case the live path
    has to decline rather than guess at.
    """

    is_live: ClassVar[bool] = True

    def __init__(self, filters: ExchangeFilters | None = None, fail_for: str | None = None) -> None:
        self.filters = filters or ExchangeFilters(
            step_size=Decimal("0.00000001"),
            min_qty=Decimal("0.00000001"),
            min_notional=Decimal("1"),
            tick_size=Decimal("0.01"),
        )
        self.fail_for = fail_for.upper() if fail_for else None
        self.calls: list[str] = []

    def get(self, symbol: str, *, force: bool = False) -> ExchangeFilters:
        self.calls.append(symbol.upper())
        if self.fail_for is not None and symbol.upper() == self.fail_for:
            raise ExchangeInfoError(f"exchangeInfo unavailable for {symbol}")
        return self.filters
