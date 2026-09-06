"""Strategy generation (AI-01, AI-05, AI-06).

The model returns structured data which is validated against `StrategySpec` before it
becomes anything. LLM output is untrusted input: parsed against a strict schema, never
executed as code, never interpolated into a query or an order (AI-06).

The client is injected so the loop runs against a stub in tests and so total loss of the
AI layer degrades to "no new candidates" rather than a crash (AI-08).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Protocol

from atlas.data.models import Timeframe
from atlas.errors import ConfigurationError
from atlas.strategy.indicators import INDICATOR_NAMES
from atlas.strategy.spec import StrategySpec

MODEL_ID = "claude-opus-5"
MAX_TOKENS = 16_000


class SpecClient(Protocol):
    """Returns a StrategySpec for a prompt, or raises."""

    model_id: str

    def generate(self, system: str, user: str) -> StrategySpec: ...


@dataclass(frozen=True)
class GenerationRecord:
    """Audit payload for one generation attempt (AI-07)."""

    model_id: str
    prompt_hash: str
    output_hash: str
    accepted: bool
    reason: str

    def as_dict(self) -> dict[str, object]:
        return {
            "model_id": self.model_id,
            "prompt_hash": self.prompt_hash,
            "output_hash": self.output_hash,
            "accepted": self.accepted,
            "reason": self.reason,
        }


def hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class AnthropicSpecClient:
    """Anthropic-backed generator.

    Imported lazily so the package works without the SDK installed: the control plane
    must not depend on the advisory plane being available (AI-08).
    """

    def __init__(self, api_key: str | None = None, model_id: str = MODEL_ID) -> None:
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover - exercised by absence
            raise ConfigurationError(
                "the anthropic SDK is not installed; install atlas[research] to run "
                "the research loop. The control plane does not require it."
            ) from exc

        self.model_id = model_id
        self._client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()

    def generate(self, system: str, user: str) -> StrategySpec:
        response = self._client.messages.parse(
            model=self.model_id,
            max_tokens=MAX_TOKENS,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_format=StrategySpec,
        )
        parsed = response.parsed_output
        # AI-06: model output is untrusted. The SDK's parse helper is trusted to have
        # validated the schema, but the type is asserted here too - a research-plane
        # value reaching the pipeline as anything other than a StrategySpec would be a
        # containment failure, not a parsing inconvenience.
        if not isinstance(parsed, StrategySpec):
            raise ValueError(f"model returned {type(parsed).__name__}, not a StrategySpec")
        return parsed


def validate_generated_spec(
    spec: StrategySpec, symbol: str, timeframe: Timeframe
) -> tuple[bool, str]:
    """Post-generation checks the schema alone cannot express.

    The schema guarantees a well-formed spec; this guarantees it is the spec we asked
    for. A model that quietly substitutes a different symbol would otherwise have its
    output backtested against the wrong market.
    """
    if spec.symbol.upper() != symbol.upper():
        return False, f"symbol mismatch: asked for {symbol}, got {spec.symbol}"
    if spec.timeframe is not timeframe:
        return False, f"timeframe mismatch: asked for {timeframe}, got {spec.timeframe}"
    if not spec.entries:
        return False, "specification defines no entry rule"

    declared = {item.name for item in spec.indicators}
    unknown = declared - set(INDICATOR_NAMES)
    if unknown:
        return False, f"specification names unavailable indicators: {sorted(unknown)}"
    return True, "accepted"
