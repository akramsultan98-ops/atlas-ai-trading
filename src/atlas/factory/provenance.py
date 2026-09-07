"""Configuration provenance for factory results.

Two backtest results are only comparable when they were produced under the same rules.
A strategy measured with a 0.05% fee and a $1 minimum notional will look better than an
identical strategy measured with 0.10% and $10, and nothing in the stored numbers says
so. The configuration hash makes that difference visible: results with different hashes
were not measured against the same yardstick and must not be ranked against each other.

It covers what changes the *outcome* of a run — risk policy, exchange filters, cost
model, starting equity — and nothing else. Including anything cosmetic would make the
hash churn and stop meaning anything.
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal

from atlas.backtest.costs import CostModel
from atlas.risk.sizing import ExchangeFilters, SizingPolicy

CONFIG_VERSION = "1"


def config_hash(
    *,
    policy: SizingPolicy,
    filters: ExchangeFilters,
    costs: CostModel,
    starting_equity: Decimal,
) -> str:
    """Deterministic hash of everything that can change a backtest's numbers."""
    canonical = json.dumps(
        {
            "version": CONFIG_VERSION,
            "policy": {
                "risk_pct": str(policy.risk_pct),
                "max_position_pct": str(policy.max_position_pct),
                "max_deployed_pct": str(policy.max_deployed_pct),
                "max_concurrent": policy.max_concurrent,
            },
            "filters": {
                "step_size": str(filters.step_size),
                "min_qty": str(filters.min_qty),
                "min_notional": str(filters.min_notional),
                "tick_size": str(filters.tick_size),
            },
            "costs": {
                "fee_rate": str(costs.fee_rate),
                "slippage_rate": str(costs.slippage_rate),
            },
            "starting_equity": str(starting_equity),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


__all__ = ["CONFIG_VERSION", "config_hash"]
