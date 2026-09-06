"""Telegram alerting (OPS).

Outbound only. There is deliberately no command handler, no polling loop and no webhook
receiver: an inbound channel that could change system state would be an unauthenticated
path into the control plane, and David uses Telegram purely to be told things
[21:42-21:51].
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from atlas.models import ExchangeEnv


class Severity(StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


@dataclass(frozen=True)
class Alert:
    severity: Severity
    title: str
    body: str
    exchange_env: ExchangeEnv

    def render(self) -> str:
        """Every alert names its environment (EXEC-01).

        Reading a testnet alert as live, or the reverse, is the kind of mistake that
        happens at 3am.
        """
        marker = {"INFO": "-", "WARNING": "!", "CRITICAL": "!!"}[str(self.severity)]
        return f"[{marker}] ATLAS [{self.exchange_env}] {self.title}\n\n{self.body}"


class NotifyTransport(Protocol):
    def post(self, url: str, payload: dict[str, Any]) -> Any: ...


class UrllibNotifyTransport:
    def __init__(self, timeout: float = 10.0) -> None:
        self.timeout = timeout

    def post(self, url: str, payload: dict[str, Any]) -> Any:
        data = urllib.parse.urlencode(payload).encode("utf-8")
        request = urllib.request.Request(url, data=data)
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return json.loads(response.read().decode("utf-8"))


class TelegramNotifier:
    """Sends alerts. Never receives."""

    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        transport: NotifyTransport | None = None,
    ) -> None:
        self._token = bot_token
        self._chat_id = chat_id
        self._transport = transport or UrllibNotifyTransport()

    def send(self, alert: Alert) -> bool:
        """Best-effort delivery. A failed alert must never stop trading or retirement."""
        try:
            self._transport.post(
                f"https://api.telegram.org/bot{self._token}/sendMessage",
                {"chat_id": self._chat_id, "text": alert.render()},
            )
        except Exception:
            return False
        return True
