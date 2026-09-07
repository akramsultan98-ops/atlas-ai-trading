"""Research provider selection and the credential boundary (AI-01, AI-08)."""

from __future__ import annotations

from pathlib import Path

import pytest

from atlas.cli import main
from atlas.config import Settings
from atlas.research.generator import MODEL_ID
from atlas.research.provider import (
    API_KEY_VAR,
    KNOWN_PROVIDERS,
    ResearchProviderUnavailable,
    build_spec_client,
)

RISK_ENV = {
    "ATLAS_RISK_PCT": "0.01",
    "ATLAS_MAX_CONCURRENT": "3",
    "ATLAS_MAX_POSITION_PCT": "0.3333",
    "ATLAS_MAX_DEPLOYED_PCT": "0.75",
    "ATLAS_DAILY_LOSS_LIMIT": "0.05",
    "ATLAS_MAX_ACCOUNT_DD": "0.20",
}


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for key, value in RISK_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("ATLAS_DATA_DIR", str(tmp_path / "var"))
    monkeypatch.delenv("ATLAS_RESEARCH_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)


def test_a_missing_credential_names_the_variable_to_set(env: None) -> None:
    """The boundary has to be actionable, not just closed."""
    with pytest.raises(ResearchProviderUnavailable, match=API_KEY_VAR):
        build_spec_client(Settings())


def test_it_refuses_to_substitute_the_exchange_credential(
    env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A model provider reachable with an exchange key makes injection an order path."""
    monkeypatch.setenv("ATLAS_BINANCE_API_KEY", "exchange-key")
    monkeypatch.setenv("ATLAS_BINANCE_API_SECRET", "exchange-secret")

    with pytest.raises(ResearchProviderUnavailable):
        build_spec_client(Settings())


def test_an_unknown_provider_is_refused(env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATLAS_RESEARCH_PROVIDER", "some-other-llm")
    monkeypatch.setenv("ATLAS_RESEARCH_API_KEY", "k")
    with pytest.raises(ResearchProviderUnavailable, match="unknown research provider"):
        build_spec_client(Settings())


def test_anthropic_is_a_known_provider() -> None:
    assert "anthropic" in KNOWN_PROVIDERS


def test_the_research_plane_never_holds_exchange_credentials(
    env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AI-01, enforced by absence rather than by instruction."""
    monkeypatch.setenv("ATLAS_BINANCE_API_KEY", "exchange-key")
    monkeypatch.setenv("ATLAS_BINANCE_API_SECRET", "exchange-secret")
    monkeypatch.setenv("ATLAS_RESEARCH_API_KEY", "research-key")

    research = Settings().for_research_plane()
    assert research.binance_api_key is None
    assert research.binance_api_secret is None
    assert research.has_research_credentials()


def test_the_execution_plane_never_holds_the_research_credential(
    env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The mirror: a key the trading process does not hold is one it cannot leak."""
    monkeypatch.setenv("ATLAS_BINANCE_API_KEY", "exchange-key")
    monkeypatch.setenv("ATLAS_BINANCE_API_SECRET", "exchange-secret")
    monkeypatch.setenv("ATLAS_RESEARCH_API_KEY", "research-key")

    execution = Settings().for_execution_plane()
    assert execution.research_api_key is None
    assert execution.has_exchange_credentials()


def test_the_model_defaults_but_is_overridable(env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATLAS_RESEARCH_API_KEY", "k")
    assert Settings().research_model == ""  # empty means "provider default"
    monkeypatch.setenv("ATLAS_RESEARCH_MODEL", "some-model")
    assert Settings().research_model == "some-model"
    assert MODEL_ID  # a default exists to fall back to


def test_describe_reports_credential_presence_not_the_secret(
    env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ATLAS_RESEARCH_API_KEY", "super-secret-value")
    described = Settings().describe()

    assert described["research_credentials"] == "present"
    assert "super-secret-value" not in str(described)


# ------------------------------------------------------------------ CLI surface


def test_provider_command_reports_the_missing_credential(
    env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["research", "provider"]) == 1
    out = capsys.readouterr().out
    assert "ABSENT" in out
    assert API_KEY_VAR in out


def test_provider_command_confirms_no_exchange_key_is_present(
    env: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The research plane reporting an exchange key would be the bug worth catching."""
    monkeypatch.setenv("ATLAS_BINANCE_API_KEY", "exchange-key")
    monkeypatch.setenv("ATLAS_BINANCE_API_SECRET", "exchange-secret")
    monkeypatch.setenv("ATLAS_RESEARCH_API_KEY", "research-key")

    assert main(["research", "provider"]) == 0
    out = capsys.readouterr().out
    assert "absent (correct)" in out
    assert "research-key" not in out


def test_research_run_stops_at_the_credential_boundary(
    env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """No key means no candidates, and no attempt to reach the network."""
    assert main(["research", "run"]) == 2
    assert API_KEY_VAR in capsys.readouterr().err
