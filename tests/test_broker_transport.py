"""UrllibBrokerTransport (Phase B) and failure injection (Phase P).

No network: `_request` is overridden so the retry policy itself is under test.
"""

from __future__ import annotations

import json
import urllib.error
from typing import Any

import pytest

from atlas.data.klines import MarketDataError
from atlas.execution.broker import OrderRejection, UrllibBrokerTransport, classify_rejection


def http_error(code: int, body: str) -> urllib.error.HTTPError:
    import io

    return urllib.error.HTTPError(
        "https://testnet.binance.vision/api/v3/order",
        code,
        "err",
        {},  # type: ignore[arg-type]
        io.BytesIO(body.encode()),
    )


class Scripted(UrllibBrokerTransport):
    """Replays a queue of responses or exceptions and counts attempts."""

    def __init__(self, *responses: object, **kw: Any) -> None:
        super().__init__(timeout=0.01, retries=kw.pop("retries", 3), backoff_base=0)
        self.responses = list(responses)
        self.attempts = 0

    def _request(self, method: str, url: str, headers: dict[str, str]) -> Any:
        self.attempts += 1
        item = self.responses.pop(0) if self.responses else {}
        if isinstance(item, Exception):
            raise item
        return item


URL = "https://testnet.binance.vision/api/v3/order?symbol=BTCUSDT&signature=DEADBEEF"


def test_successful_request_returns_parsed_json() -> None:
    transport = Scripted({"orderId": 1, "status": "FILLED"})
    assert transport.post(URL, {})["status"] == "FILLED"
    assert transport.attempts == 1


def test_get_and_delete_use_the_same_policy() -> None:
    assert Scripted({"a": 1}).get(URL, {}) == {"a": 1}
    assert Scripted({"b": 2}).delete(URL, {}) == {"b": 2}


# ------------------------------------------------------------------ Phase P: 4xx


def test_terminal_4xx_is_not_retried() -> None:
    """Resending an order refused on its merits burns rate limit and hides the fix."""
    body = json.dumps({"code": -2010, "msg": "Account has insufficient balance"})
    transport = Scripted(http_error(400, body))
    with pytest.raises(OrderRejection) as exc:
        transport.post(URL, {})
    assert not exc.value.retryable
    assert transport.attempts == 1, "a terminal rejection must not be retried"


def test_filter_failure_is_terminal() -> None:
    body = json.dumps({"code": -1013, "msg": "Filter failure: MIN_NOTIONAL"})
    transport = Scripted(http_error(400, body))
    with pytest.raises(OrderRejection, match="MIN_NOTIONAL"):
        transport.post(URL, {})
    assert transport.attempts == 1


def test_unauthorised_is_terminal() -> None:
    transport = Scripted(http_error(401, json.dumps({"msg": "Invalid API-key"})))
    with pytest.raises(OrderRejection) as exc:
        transport.get(URL, {})
    assert not exc.value.retryable
    assert transport.attempts == 1


# ------------------------------------------------------- Phase P: retryable faults


def test_rate_limit_is_retried_then_succeeds() -> None:
    transport = Scripted(
        http_error(429, json.dumps({"msg": "Too many requests"})),
        {"orderId": 7},
    )
    assert transport.post(URL, {})["orderId"] == 7
    assert transport.attempts == 2


def test_server_error_is_retried() -> None:
    transport = Scripted(http_error(503, "unavailable"), {"ok": True})
    assert transport.get(URL, {}) == {"ok": True}
    assert transport.attempts == 2


def test_timeout_is_retried_then_gives_up_bounded() -> None:
    transport = Scripted(TimeoutError("t"), TimeoutError("t"), TimeoutError("t"), retries=3)
    with pytest.raises(OrderRejection) as exc:
        transport.post(URL, {})
    assert exc.value.retryable
    assert transport.attempts == 3, "retries must be bounded"


def test_connection_error_is_retried() -> None:
    transport = Scripted(urllib.error.URLError("refused"), {"ok": 1})
    assert transport.get(URL, {}) == {"ok": 1}


def test_blocked_egress_surfaces_as_a_retryable_rejection() -> None:
    """The exact failure this environment produces: 403 at the tunnel, not from Binance."""
    transport = Scripted(
        urllib.error.URLError("Tunnel connection failed: 403 Forbidden"), retries=1
    )
    with pytest.raises(OrderRejection, match="403 Forbidden"):
        transport.get(URL, {})


# ------------------------------------------------------- Phase P: malformed data


def test_malformed_response_is_not_retried() -> None:
    """The exchange answered, unusably. Retrying cannot fix a parse failure."""
    transport = Scripted(json.JSONDecodeError("bad", "", 0))
    with pytest.raises(MarketDataError, match="unparseable"):
        transport.get(URL, {})
    assert transport.attempts == 1


# --------------------------------------------------------------- Phase Q: secrets


def test_signature_never_appears_in_an_error_message() -> None:
    transport = Scripted(TimeoutError("t"), TimeoutError("t"), TimeoutError("t"), retries=3)
    with pytest.raises(OrderRejection) as exc:
        transport.post(URL, {})
    message = str(exc.value)
    assert "DEADBEEF" not in message
    assert "signature" not in message
    assert "/api/v3/order" in message, "the endpoint should still be identifiable"


def test_redaction_strips_the_whole_query_string() -> None:
    redacted = UrllibBrokerTransport()._redact(
        "https://x/api/v3/account?timestamp=1&recvWindow=5000&signature=SECRET"
    )
    assert redacted == "https://x/api/v3/account"
    assert "SECRET" not in redacted


def test_classify_rejection_handles_a_non_json_body() -> None:
    rejection = classify_rejection(502, "<html>bad gateway</html>")
    assert rejection.retryable
