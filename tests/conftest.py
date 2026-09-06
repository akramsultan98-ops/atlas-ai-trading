from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from atlas.audit import AuditLog
from atlas.db.engine import Database
from atlas.killswitch import KillSwitch

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
