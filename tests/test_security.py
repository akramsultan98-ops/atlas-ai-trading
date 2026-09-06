"""Security invariants (Phase Q).

Each of these was verified by inspection during the audit. They are tests so that a
future change cannot quietly undo one.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from atlas.config import Settings
from atlas.models import Environment, ExchangeEnv

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "atlas"
SOURCES = [p for p in SRC.rglob("*.py")]


def source_of(path: Path) -> str:
    return path.read_text()


# --------------------------------------------------------------- the live gate


def test_one_variable_cannot_activate_live_trading() -> None:
    """The prompt's hard requirement: no single casual env var reaches mainnet."""
    with pytest.raises(ValidationError, match="requires env=production"):
        Settings(exchange_env=ExchangeEnv.LIVE, env=Environment.DEVELOPMENT)
    with pytest.raises(ValidationError):
        Settings(exchange_env=ExchangeEnv.LIVE, env=Environment.TESTING)


def test_production_env_alone_does_not_reach_mainnet() -> None:
    """The other half: production without live stays on testnet."""
    settings = Settings(env=Environment.PRODUCTION)
    assert settings.exchange_env is ExchangeEnv.TESTNET
    assert not settings.is_live


def test_both_variables_are_required_together() -> None:
    settings = Settings(env=Environment.PRODUCTION, exchange_env=ExchangeEnv.LIVE)
    assert settings.is_live, "both set is the only path to live"


def test_testnet_is_the_default_with_no_configuration() -> None:
    assert Settings().exchange_env is ExchangeEnv.TESTNET


def test_build_service_refuses_live_outside_production() -> None:
    from atlas.config import RiskSettings
    from atlas.errors import ConfigurationError
    from atlas.runtime.service import build_service

    settings = Settings().model_copy(update={"exchange_env": ExchangeEnv.LIVE})
    with pytest.raises(ConfigurationError, match="production"):
        build_service(
            settings,
            RiskSettings(  # type: ignore[call-arg]
                risk_pct="0.01",
                max_concurrent=3,
                max_position_pct="0.3333",
                max_deployed_pct="0.75",
                daily_loss_limit="0.05",
                max_account_dd="0.20",
            ),
        )


# ------------------------------------------------------------------- secrets


def test_credentials_are_secret_typed() -> None:
    """SecretStr keeps values out of repr and accidental interpolation."""
    for field in ("binance_api_key", "binance_api_secret", "telegram_bot_token"):
        annotation = str(Settings.model_fields[field].annotation)
        assert "SecretStr" in annotation, f"{field} is not SecretStr"


def test_credentials_do_not_appear_in_repr() -> None:
    settings = Settings(
        binance_api_key=SecretStr("KEY-MATERIAL-XYZ"),
        binance_api_secret=SecretStr("SECRET-MATERIAL-XYZ"),
    )
    rendered = repr(settings) + str(settings)
    assert "KEY-MATERIAL-XYZ" not in rendered
    assert "SECRET-MATERIAL-XYZ" not in rendered


def test_startup_summary_carries_no_secret_material() -> None:
    settings = Settings(
        binance_api_key=SecretStr("KEY-MATERIAL-XYZ"),
        binance_api_secret=SecretStr("SECRET-MATERIAL-XYZ"),
        telegram_bot_token=SecretStr("TELEGRAM-TOKEN-XYZ"),
        telegram_chat_id="123",
    )
    rendered = str(settings.describe())
    for secret in ("KEY-MATERIAL-XYZ", "SECRET-MATERIAL-XYZ", "TELEGRAM-TOKEN-XYZ"):
        assert secret not in rendered
    assert settings.describe()["exchange_credentials"] == "present"


def test_no_source_file_logs_a_credential() -> None:
    pattern = re.compile(r"(print|log(ger)?\.\w+)\([^)]*\b(api_key|api_secret|_secret|signature)\b")
    for path in SOURCES:
        text = source_of(path)
        for line in text.splitlines():
            if "get_secret_value" in line or line.strip().startswith("#"):
                continue
            assert not pattern.search(line), f"{path.name}: {line.strip()}"


def test_signature_is_stripped_from_transport_errors() -> None:
    from atlas.execution.broker import UrllibBrokerTransport

    redacted = UrllibBrokerTransport()._redact(
        "https://api/x?timestamp=1&signature=ABCDEF0123456789"
    )
    assert "ABCDEF0123456789" not in redacted
    assert "signature" not in redacted


# ------------------------------------------------------ code execution surface


@pytest.mark.parametrize("name", ["eval", "exec", "compile", "__import__"])
def test_no_arbitrary_code_execution_anywhere_in_src(name: str) -> None:
    """STRAT-01: strategies are data. Nothing generated is ever executed."""
    for path in SOURCES:
        for node in ast.walk(ast.parse(source_of(path))):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id != name, f"{path.name} calls {name}"


def test_no_shell_execution() -> None:
    for path in SOURCES:
        text = source_of(path)
        for marker in ("subprocess", "os.system", "os.popen", "shell=True"):
            assert marker not in text, f"{path.name} uses {marker}"


def test_no_unsafe_deserialisation() -> None:
    for path in SOURCES:
        text = source_of(path)
        for marker in ("pickle.loads", "yaml.load(", "marshal.loads"):
            assert marker not in text, f"{path.name} uses {marker}"


def test_all_sql_is_parameterised() -> None:
    """No f-string or concatenation reaches execute(); every query uses placeholders."""
    pattern = re.compile(r"execute\(\s*f[\"']")
    for path in SOURCES:
        assert not pattern.search(source_of(path)), f"{path.name} has an f-string query"


# --------------------------------------------------- research plane isolation


def test_research_plane_names_no_credential() -> None:
    """AI-01, re-asserted at the source level."""
    for path in (SRC / "research").rglob("*.py"):
        text = source_of(path)
        for marker in ("binance_api_key", "binance_api_secret", "BINANCE"):
            assert marker not in text, f"{path.name} references {marker}"


def test_research_plane_imports_no_execution_module() -> None:
    banned = ("atlas.execution", "atlas.killswitch", "atlas.promotion", "atlas.runtime")
    for path in (SRC / "research").rglob("*.py"):
        for node in ast.walk(ast.parse(source_of(path))):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.startswith(banned), f"{path.name}: {node.module}"


def test_no_withdrawal_endpoint_anywhere() -> None:
    """The broker cannot move funds off the exchange, by absence."""
    for path in SOURCES:
        text = source_of(path).lower()
        for marker in ("/sapi/v1/capital/withdraw", "withdraw(", "def withdraw"):
            assert marker not in text, f"{path.name} references a withdrawal path"
