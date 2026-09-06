"""The advisory plane's tool surface (AI-02, AI-04).

Enforcement is *absence*. The functions the AI must never reach are not registered
here, so there is nothing to call — as opposed to a prompt instructing it not to,
which is a request a capable, confused or compromised model can route around.

Authority is one-way toward safety (AI-04): the AI may retire or disable a strategy,
which only reduces exposure, and cannot enable, resume, promote or scale one up.
"""

from __future__ import annotations

from typing import Final

ALLOWED_TOOLS: Final[frozenset[str]] = frozenset(
    {
        "research_market_concepts",
        "propose_strategy_spec",
        "request_backtest",
        "read_backtest_result",
        "read_selection_result",
        "read_incubation_metrics",
        "read_live_metrics",
        "request_strategy_retirement",
        "draft_report",
    }
)

# Named explicitly so the boundary is greppable and a regression is a failing test
# rather than a silent addition. Registering any of these is a containment breach.
FORBIDDEN_TOOLS: Final[frozenset[str]] = frozenset(
    {
        "place_order",
        "cancel_order",
        "amend_order",
        "size_position",
        "set_risk_parameter",
        "set_risk_limits",
        "promote_strategy",
        "approve_promotion",
        "reactivate_strategy",
        "resume_strategy",
        "arm_kill_switch",
        "disarm_kill_switch",
        "transfer_funds",
        "withdraw",
        "read_api_credentials",
        "set_exchange_env",
    }
)


class ContainmentBreach(RuntimeError):
    """A tool outside the advisory allowlist was registered or invoked."""


def assert_tool_allowed(name: str) -> None:
    """Gate every advisory-plane tool invocation. Raises on anything unlisted."""
    if name in FORBIDDEN_TOOLS:
        raise ContainmentBreach(
            f"tool {name!r} belongs to the control plane and is never reachable from "
            "the advisory plane (AI-02)"
        )
    if name not in ALLOWED_TOOLS:
        raise ContainmentBreach(
            f"tool {name!r} is not on the advisory allowlist; permitted: {sorted(ALLOWED_TOOLS)}"
        )


def validate_tool_registry(names: frozenset[str] | set[str]) -> None:
    """Check a registry at construction time, before any model can call into it."""
    overlap = set(names) & FORBIDDEN_TOOLS
    if overlap:
        raise ContainmentBreach(
            f"control-plane tools registered on the advisory surface: {sorted(overlap)}"
        )
    unknown = set(names) - ALLOWED_TOOLS
    if unknown:
        raise ContainmentBreach(f"unknown tools on the advisory surface: {sorted(unknown)}")
