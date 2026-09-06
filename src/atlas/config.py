"""Typed configuration.

No financial parameter has a default (ADR-006). A missing risk value is a startup error,
not a fallback: a default risk parameter is a silent policy nobody chose.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Self

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from atlas.errors import ConfigurationError
from atlas.models import Environment, ExchangeEnv

_ENV_FILE = ".env"


class RiskSettings(BaseSettings):
    """Risk policy — specification section 6.

    Every value here is an ATLAS decision. The source video supplies no sizing or risk
    policy at all; its only reference is "percentage of portfolio" in passing [17:30].
    None of these numbers are attributable to David.
    """

    model_config = SettingsConfigDict(
        env_prefix="ATLAS_", env_file=_ENV_FILE, extra="ignore", frozen=True
    )

    # No defaults. Absence is an error.
    risk_pct: Decimal = Field(description="RISK-01 fraction of equity risked per trade")
    max_concurrent: int = Field(description="RISK-02 maximum simultaneous open positions")
    max_position_pct: Decimal = Field(description="RISK-03 cap on one position vs equity")
    max_deployed_pct: Decimal = Field(description="RISK-04 cap on total deployed capital")
    daily_loss_limit: Decimal = Field(description="RISK-05 daily loss fraction arming kill switch")
    max_account_dd: Decimal = Field(description="RISK-06 drawdown from peak arming kill switch")

    @model_validator(mode="after")
    def _validate_policy(self) -> Self:
        fractions = {
            "risk_pct": self.risk_pct,
            "max_position_pct": self.max_position_pct,
            "max_deployed_pct": self.max_deployed_pct,
            "daily_loss_limit": self.daily_loss_limit,
            "max_account_dd": self.max_account_dd,
        }
        for name, value in fractions.items():
            if not (Decimal(0) < value < Decimal(1)):
                raise ValueError(f"{name} must be in (0, 1), got {value}")

        if self.max_concurrent < 1:
            raise ValueError(f"max_concurrent must be >= 1, got {self.max_concurrent}")

        # A single position must be openable at its own cap.
        if self.max_position_pct > self.max_deployed_pct:
            raise ValueError(
                f"max_position_pct ({self.max_position_pct}) exceeds max_deployed_pct "
                f"({self.max_deployed_pct}); a position could never open at its cap"
            )

        # Note: max_position_pct * max_concurrent MAY exceed max_deployed_pct. The caps
        # bind at different levels and the aggregate cap is deliberately tighter.
        return self

    @property
    def min_feasible_stop_distance(self) -> Decimal:
        """Smallest stop distance that can be sized at full intended risk.

        Derivation (specification section 6): a position sized to risk exactly
        `equity * risk_pct` over a stop of fractional distance `d` has notional
        `equity * risk_pct / d`. Capping notional at `equity * max_position_pct` and
        solving for the `d` at which the cap begins to bind gives:

            d_min = risk_pct / max_position_pct

        Below `d_min` the cap binds and the position is under-risked (safe, but not the
        intended exposure). This bound is structural: equity cancels, so it does not
        move as the account grows.
        """
        return self.risk_pct / self.max_position_pct


class Settings(BaseSettings):
    """Root configuration."""

    model_config = SettingsConfigDict(
        env_prefix="ATLAS_", env_file=_ENV_FILE, extra="ignore", frozen=True
    )

    env: Environment = Environment.DEVELOPMENT
    data_dir: Path = Path("var")
    log_level: str = "INFO"

    exchange_env: ExchangeEnv = ExchangeEnv.TESTNET
    quote_asset: str = "USDT"

    # Execution plane only. Never read by the research plane (AI-01).
    binance_api_key: SecretStr | None = None
    binance_api_secret: SecretStr | None = None

    @model_validator(mode="after")
    def _guard_live(self) -> Self:
        if self.exchange_env is ExchangeEnv.LIVE and self.env is not Environment.PRODUCTION:
            raise ValueError(
                "exchange_env=live requires env=production; refusing to arm a live "
                "exchange connection from a non-production environment"
            )
        return self

    @property
    def db_path(self) -> Path:
        return self.data_dir / "atlas.db"

    @property
    def killswitch_path(self) -> Path:
        return self.data_dir / "killswitch.json"

    @property
    def is_live(self) -> bool:
        return self.exchange_env is ExchangeEnv.LIVE

    def has_exchange_credentials(self) -> bool:
        return self.binance_api_key is not None and self.binance_api_secret is not None

    def for_research_plane(self) -> Settings:
        """Return settings with exchange credentials stripped (AI-01).

        The research process must never hold Binance credentials. This enforces that in
        code: the values are absent, not merely forbidden by instruction. A prompt
        telling a model not to trade is a request; a process without a key cannot trade.
        """
        return self.model_copy(update={"binance_api_key": None, "binance_api_secret": None})

    def ensure_data_dir(self) -> Path:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        return self.data_dir


def load_settings() -> tuple[Settings, RiskSettings]:
    """Load and validate all configuration, or fail loudly."""
    try:
        return Settings(), RiskSettings()  # type: ignore[call-arg]
    except Exception as exc:  # pydantic ValidationError and friends
        raise ConfigurationError(f"configuration invalid: {exc}") from exc
