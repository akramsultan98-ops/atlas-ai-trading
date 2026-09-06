"""Binance spot broker (EXEC-01, EXEC-06, EXEC-07, EXEC-09, EXEC-10).

Spot only. No margin, no futures, no withdrawal endpoint is reachable from this module —
the methods do not exist, so a bug cannot reach one.

The kill switch is checked immediately before every transmission that opens or increases
exposure (EXEC-06), not at signal time: the gap between deciding and sending is exactly
where a limit breach can land.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.parse
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Any, Protocol

from atlas.audit import AuditLog
from atlas.errors import AtlasError
from atlas.killswitch import KillSwitch
from atlas.models import AuditEventType, ExchangeEnv, OrderSide

SPOT_MAINNET = "https://api.binance.com"
SPOT_TESTNET = "https://testnet.binance.vision"

RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})
EXECUTION_ACTOR = "execution"


class OrderRole(StrEnum):
    ENTRY = "ENTRY"
    STOP = "STOP"
    TARGET = "TARGET"


class OrderRejection(AtlasError):
    """The exchange refused an order."""

    def __init__(self, message: str, *, retryable: bool, status: int | None = None) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.status = status


@dataclass(frozen=True)
class OrderRequest:
    client_order_id: str
    symbol: str
    side: OrderSide
    role: OrderRole
    quantity: Decimal
    price: Decimal | None = None
    stop_price: Decimal | None = None

    @property
    def order_type(self) -> str:
        if self.role is OrderRole.ENTRY:
            return "MARKET"
        return "STOP_LOSS_LIMIT" if self.role is OrderRole.STOP else "LIMIT"


@dataclass(frozen=True)
class OrderAck:
    client_order_id: str
    exchange_order_id: str
    symbol: str
    status: str
    executed_qty: Decimal
    raw: dict[str, Any]


class BrokerTransport(Protocol):
    """Signed HTTP contract. Implementations must apply a timeout."""

    def post(self, url: str, headers: dict[str, str]) -> Any: ...
    def get(self, url: str, headers: dict[str, str]) -> Any: ...
    def delete(self, url: str, headers: dict[str, str]) -> Any: ...


class BinanceSpotBroker:
    """Signed Binance spot client.

    Credentials are supplied by the execution process only. The research plane never
    constructs this class — it holds no key to pass (AI-01).
    """

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        killswitch: KillSwitch,
        audit: AuditLog,
        *,
        exchange_env: ExchangeEnv = ExchangeEnv.TESTNET,
        transport: BrokerTransport | None = None,
        recv_window_ms: int = 5000,
    ) -> None:
        if not api_key or not api_secret:
            raise AtlasError("broker requires both an API key and secret")
        self._key = api_key
        self._secret = api_secret.encode("utf-8")
        self._killswitch = killswitch
        self._audit = audit
        self.exchange_env = exchange_env
        self.base_url = SPOT_MAINNET if exchange_env is ExchangeEnv.LIVE else SPOT_TESTNET
        self._transport = transport
        self._recv_window = recv_window_ms

    # ---------------------------------------------------------------- signing

    def _signed_query(self, params: dict[str, Any]) -> str:
        payload = {**params, "timestamp": int(time.time() * 1000), "recvWindow": self._recv_window}
        query = urllib.parse.urlencode(payload)
        signature = hmac.new(self._secret, query.encode("utf-8"), hashlib.sha256).hexdigest()
        return f"{query}&signature={signature}"

    def _headers(self) -> dict[str, str]:
        return {"X-MBX-APIKEY": self._key, "User-Agent": "atlas/0.1"}

    def _require_transport(self) -> BrokerTransport:
        if self._transport is None:
            raise AtlasError(
                "no broker transport configured; refusing to construct a live HTTP "
                "client implicitly"
            )
        return self._transport

    # ---------------------------------------------------------------- orders

    def place(self, request: OrderRequest) -> OrderAck:
        """Transmit one order. Checks the kill switch immediately beforehand (EXEC-06).

        Protective orders (stop, target) are permitted while armed: refusing them would
        leave an open position naked, which is the opposite of what arming is for
        (KILL-03).
        """
        if request.role is OrderRole.ENTRY:
            self._killswitch.check_can_enter()
        else:
            self._killswitch.check_can_place_protective()

        params: dict[str, Any] = {
            "symbol": request.symbol.upper(),
            "side": str(request.side),
            "type": request.order_type,
            "quantity": str(request.quantity),
            "newClientOrderId": request.client_order_id,
        }
        if request.price is not None:
            params["price"] = str(request.price)
            params["timeInForce"] = "GTC"
        if request.stop_price is not None:
            params["stopPrice"] = str(request.stop_price)

        # EXEC-10: log the intent before the network call, so an order that vanishes
        # mid-flight still leaves a record that it was attempted.
        self._audit.append(
            AuditEventType.ORDER_INTENT,
            {
                "client_order_id": request.client_order_id,
                "symbol": request.symbol,
                "side": str(request.side),
                "role": str(request.role),
                "quantity": str(request.quantity),
                "exchange_env": str(self.exchange_env),
            },
            EXECUTION_ACTOR,
        )

        url = f"{self.base_url}/api/v3/order?{self._signed_query(params)}"
        try:
            raw = self._require_transport().post(url, self._headers())
        except OrderRejection as exc:
            self._audit.append(
                AuditEventType.ORDER_RESULT,
                {
                    "client_order_id": request.client_order_id,
                    "accepted": False,
                    "retryable": exc.retryable,
                    "error": str(exc),
                },
                EXECUTION_ACTOR,
            )
            raise

        ack = OrderAck(
            client_order_id=str(raw.get("clientOrderId", request.client_order_id)),
            exchange_order_id=str(raw.get("orderId", "")),
            symbol=str(raw.get("symbol", request.symbol)),
            status=str(raw.get("status", "UNKNOWN")),
            executed_qty=Decimal(str(raw.get("executedQty", "0"))),
            raw=raw,
        )
        self._audit.append(
            AuditEventType.ORDER_RESULT,
            {
                "client_order_id": ack.client_order_id,
                "exchange_order_id": ack.exchange_order_id,
                "status": ack.status,
                "executed_qty": str(ack.executed_qty),
                "accepted": True,
            },
            EXECUTION_ACTOR,
        )
        return ack

    def cancel(self, symbol: str, client_order_id: str) -> dict[str, Any]:
        """Cancel by client order id. Always permitted: cancelling reduces exposure."""
        query = self._signed_query({"symbol": symbol.upper(), "origClientOrderId": client_order_id})
        result = self._require_transport().delete(
            f"{self.base_url}/api/v3/order?{query}", self._headers()
        )
        return dict(result) if isinstance(result, dict) else {}

    def open_orders(self, symbol: str | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"symbol": symbol.upper()} if symbol else {}
        raw = self._require_transport().get(
            f"{self.base_url}/api/v3/openOrders?{self._signed_query(params)}", self._headers()
        )
        return list(raw) if isinstance(raw, list) else []

    def account_balances(self) -> dict[str, Decimal]:
        raw = self._require_transport().get(
            f"{self.base_url}/api/v3/account?{self._signed_query({})}", self._headers()
        )
        if not isinstance(raw, dict):
            return {}
        return {
            str(b["asset"]): Decimal(str(b["free"]))
            for b in raw.get("balances", [])
            if Decimal(str(b.get("free", "0"))) > 0
        }


def classify_rejection(status: int, body: str) -> OrderRejection:
    """EXEC-09: retryable (rate limit, timeout, 5xx) vs terminal (filters, balance).

    Terminal rejections never retry: resending an order the exchange has already
    refused on its merits just burns rate limit and delays the real fix.
    """
    retryable = status in RETRYABLE_STATUS
    try:
        message = str(json.loads(body).get("msg", body))
    except (json.JSONDecodeError, AttributeError):
        message = body
    return OrderRejection(
        f"exchange rejected order (HTTP {status}): {message}",
        retryable=retryable,
        status=status,
    )
