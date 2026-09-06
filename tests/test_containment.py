"""AI-01..08. The load-bearing safety tests.

These assert the boundary is structural — credentials absent, tools unregistered,
database read-only — rather than a matter of the model behaving well.
"""

from __future__ import annotations

import ast
import sqlite3
from pathlib import Path

import pytest

from atlas.config import Settings
from atlas.db.engine import Database, connect_readonly
from atlas.research.tools import (
    ALLOWED_TOOLS,
    FORBIDDEN_TOOLS,
    ContainmentBreach,
    assert_tool_allowed,
    validate_tool_registry,
)

RESEARCH_PACKAGE = Path(__file__).resolve().parents[1] / "src" / "atlas" / "research"


# ------------------------------------------------------------------ AI-01 credentials


def test_research_plane_settings_carry_no_credentials(risk_env: dict[str, str]) -> None:
    settings = Settings(
        binance_api_key="live-key",  # type: ignore[arg-type]
        binance_api_secret="live-secret",  # type: ignore[arg-type]
    )
    research = settings.for_research_plane()
    assert research.binance_api_key is None
    assert research.binance_api_secret is None
    assert not research.has_exchange_credentials()


def test_research_modules_never_read_exchange_credentials() -> None:
    """Static check: no module in the advisory plane names a credential field."""
    for path in RESEARCH_PACKAGE.rglob("*.py"):
        source = path.read_text()
        for forbidden in ("binance_api_key", "binance_api_secret", "has_exchange_credentials"):
            assert forbidden not in source, f"{path.name} references {forbidden}"


def test_research_package_does_not_import_execution_modules() -> None:
    """AI-02 at the import level: the advisory plane cannot reach order placement.

    Parsing imports rather than grepping text, so a comment mentioning a module does
    not fail the test and an aliased import cannot slip past it.
    """
    banned_prefixes = ("atlas.execution", "atlas.killswitch", "atlas.promotion")
    for path in RESEARCH_PACKAGE.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.startswith(banned_prefixes), (
                    f"{path.name} imports {node.module}"
                )
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith(banned_prefixes), (
                        f"{path.name} imports {alias.name}"
                    )


# ------------------------------------------------------------------ AI-02 tool surface


def test_forbidden_tools_are_not_on_the_allowlist() -> None:
    assert set() == ALLOWED_TOOLS & FORBIDDEN_TOOLS


@pytest.mark.parametrize("tool", sorted(FORBIDDEN_TOOLS))
def test_every_control_plane_tool_is_refused(tool: str) -> None:
    with pytest.raises(ContainmentBreach, match="control plane"):
        assert_tool_allowed(tool)


@pytest.mark.parametrize("tool", sorted(ALLOWED_TOOLS))
def test_every_advisory_tool_is_permitted(tool: str) -> None:
    assert_tool_allowed(tool)


def test_unlisted_tool_is_refused() -> None:
    """Default deny: a tool nobody thought about is not permitted by omission."""
    with pytest.raises(ContainmentBreach, match="not on the advisory allowlist"):
        assert_tool_allowed("some_new_capability")


def test_registry_validation_rejects_control_plane_tools() -> None:
    with pytest.raises(ContainmentBreach, match="control-plane tools registered"):
        validate_tool_registry({"draft_report", "place_order"})


def test_registry_validation_accepts_the_allowlist() -> None:
    validate_tool_registry(set(ALLOWED_TOOLS))


def test_order_and_kill_switch_verbs_are_all_forbidden() -> None:
    """Guards against someone adding a tool that reaches funds without noticing."""
    for verb in (
        "place_order",
        "cancel_order",
        "amend_order",
        "disarm_kill_switch",
        "promote_strategy",
        "reactivate_strategy",
        "withdraw",
        "transfer_funds",
    ):
        assert verb in FORBIDDEN_TOOLS


# ------------------------------------------------------------------ AI-03 database


def test_research_plane_database_role_is_read_only(db: Database) -> None:
    """Enforced by SQLite, not by the caller remembering to behave."""
    db.connection.execute(
        "INSERT INTO strategy_specs(spec_hash, payload, created_at) VALUES (?, '{}', '2026-01-01')",
        ("a" * 64,),
    )
    ro = connect_readonly(db.path)
    try:
        assert ro.execute("SELECT COUNT(*) AS n FROM strategy_specs").fetchone()["n"] == 1
        for statement, params in (
            (
                "INSERT INTO strategies(id, spec_hash, symbol, timeframe, status, created_at, "
                "status_at) VALUES ('x', ?, 'BTCUSDT', '1h', 'LIVE', '2026-01-01', '2026-01-01')",
                ("a" * 64,),
            ),
            ("UPDATE kill_switch_state SET state = 'DISARMED' WHERE id = 1", ()),
            ("DELETE FROM audit_events", ()),
        ):
            with pytest.raises(sqlite3.OperationalError):
                ro.execute(statement, params)
    finally:
        ro.close()


# ------------------------------------------------------------------ AI-08 degradation


def test_control_plane_imports_without_the_anthropic_sdk() -> None:
    """AI-08: losing the AI layer stops candidate production and nothing else."""
    import importlib.util

    assert importlib.util.find_spec("anthropic") is None, (
        "this test is only meaningful while the SDK is absent"
    )
    for module in (
        "atlas.killswitch",
        "atlas.execution",
        "atlas.risk.sizing",
        "atlas.backtest.engine",
        "atlas.monitor",
    ):
        spec = importlib.util.find_spec(module)
        if spec is None:
            continue  # module not yet implemented at this phase
        importlib.import_module(module)


def test_research_generator_imports_without_the_sdk() -> None:
    """The module must import; only constructing the client should fail."""
    from atlas.research import generator

    assert generator.MODEL_ID == "claude-opus-5"
