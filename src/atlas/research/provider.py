"""Selecting a research provider from configuration (AI-01, AI-08).

`SpecClient` is already the provider interface: anything that turns a prompt into a
validated `StrategySpec` satisfies it. This module only chooses an implementation from
settings, so adding a second provider is a registry entry rather than a change to the
pipeline.

Two properties are deliberate.

The research credential is separate from the exchange credential and neither process
holds the other's. A model provider reachable with an exchange key would make prompt
injection an execution path.

A missing key is not an error at import or construction time — it is an error at the
moment generation is attempted, with a message naming the variable to set. The control
plane must keep running with no research provider configured at all (AI-08).
"""

from __future__ import annotations

from typing import Final

from atlas.config import Settings
from atlas.errors import ConfigurationError
from atlas.research.generator import MODEL_ID, AnthropicSpecClient, SpecClient

ANTHROPIC: Final = "anthropic"
KNOWN_PROVIDERS: Final[frozenset[str]] = frozenset({ANTHROPIC})

# The variable an operator has to set. Named once, here, so the error message and the
# documentation cannot drift apart.
API_KEY_VAR: Final = "ATLAS_RESEARCH_API_KEY"
PROVIDER_VAR: Final = "ATLAS_RESEARCH_PROVIDER"
MODEL_VAR: Final = "ATLAS_RESEARCH_MODEL"


class ResearchProviderUnavailable(ConfigurationError):
    """No usable research provider is configured."""


def build_spec_client(settings: Settings) -> SpecClient:
    """Construct the configured provider, or explain exactly what is missing."""
    provider = settings.research_provider.strip().lower()
    if provider not in KNOWN_PROVIDERS:
        raise ResearchProviderUnavailable(
            f"unknown research provider {provider!r}; set {PROVIDER_VAR} to one of "
            f"{sorted(KNOWN_PROVIDERS)}"
        )

    if not settings.has_research_credentials():
        raise ResearchProviderUnavailable(
            f"no research credential configured; set {API_KEY_VAR} to generate "
            "candidates. ATLAS will not use the Binance credential for this, and will "
            "not invent one."
        )

    assert settings.research_api_key is not None  # narrowed by has_research_credentials
    model = settings.research_model.strip() or MODEL_ID
    return AnthropicSpecClient(api_key=settings.research_api_key.get_secret_value(), model_id=model)


__all__ = [
    "ANTHROPIC",
    "API_KEY_VAR",
    "KNOWN_PROVIDERS",
    "MODEL_VAR",
    "PROVIDER_VAR",
    "ResearchProviderUnavailable",
    "build_spec_client",
]
