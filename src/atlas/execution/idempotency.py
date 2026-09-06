"""Deterministic client order IDs (EXEC-03).

An order's identity is derived from the decision that produced it, not from when the
process happened to send it. Replaying a signal after a crash therefore collides with
the original order at the exchange instead of opening a second position.
"""

from __future__ import annotations

import hashlib
from datetime import datetime

from atlas.models import OrderSide

MAX_CLIENT_ORDER_ID = 36  # Binance spot limit


def client_order_id(strategy_id: str, signal_bar_time: datetime, side: OrderSide, role: str) -> str:
    """Build a stable id for one (strategy, bar, side, role) decision.

    Hashed rather than concatenated because a UUID strategy id plus an ISO timestamp
    exceeds the exchange's 36-character limit.
    """
    material = f"{strategy_id}|{signal_bar_time.isoformat()}|{side}|{role}"
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return f"atlas{digest[: MAX_CLIENT_ORDER_ID - 5]}"
