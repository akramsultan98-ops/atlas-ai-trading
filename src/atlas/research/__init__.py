"""Advisory plane (specification section 2). Holds no credentials, writes no orders."""

from atlas.research.generator import (
    AnthropicSpecClient,
    GenerationRecord,
    SpecClient,
    validate_generated_spec,
)
from atlas.research.loop import CandidateOutcome, ResearchLoop, Stage
from atlas.research.tools import (
    ALLOWED_TOOLS,
    FORBIDDEN_TOOLS,
    ContainmentBreach,
    assert_tool_allowed,
    validate_tool_registry,
)

__all__ = [
    "ALLOWED_TOOLS",
    "FORBIDDEN_TOOLS",
    "AnthropicSpecClient",
    "CandidateOutcome",
    "ContainmentBreach",
    "GenerationRecord",
    "ResearchLoop",
    "SpecClient",
    "Stage",
    "assert_tool_allowed",
    "validate_generated_spec",
    "validate_tool_registry",
]
