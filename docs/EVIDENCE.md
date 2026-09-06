# Evidence register

What is actually known to work, and how it is known. A component moves down this list
only on evidence, never on confidence.

| Tier | Means |
|---|---|
| **UNIT TESTED** | Exercised against scripted inputs in this repository. Proves the logic, proves nothing about the exchange. |
| **INTEGRATION TESTED** | Exercised across component boundaries, still with no network. |
| **TESTNET VERIFIED** | Exercised against `testnet.binance.vision` with real credentials and a recorded response. |
| **LIVE VERIFIED** | Exercised against `api.binance.com` with real funds. |

## Current state

**Nothing in ATLAS has ever reached TESTNET VERIFIED.** Every exchange response in every
test is a scripted stub written by ATLAS. Outbound access to `testnet.binance.vision:443`
and `api.binance.com:443` is refused by this environment's egress policy (HTTP 403 at
CONNECT), so no request has ever left this machine for Binance.

| Component | Tier | The specific thing still unproven |
|---|---|---|
| Signed request construction (HMAC, `recvWindow`, query order) | UNIT TESTED | Whether Binance accepts the signature. A single ordering or encoding mistake shows up only as `-1022`. |
| `/api/v3/time`, clock drift check | UNIT TESTED | The real drift on the deployment host. |
| `exchangeInfo` -> `ExchangeFilters` | UNIT TESTED | The real `minNotional`, `stepSize` and `tickSize` for the traded symbols. ATLAS's assumed $5 minNotional is illustrative only (RISK-09, `atlas.economics`). |
| `/api/v3/account` free and locked balances | UNIT TESTED | Field names and shape on a real account. |
| Klines fetch and validation | UNIT TESTED | Real gap and staleness behaviour on live candles. |
| Market entry (`POST /api/v3/order`) | UNIT TESTED | Whether `cummulativeQuoteQty` is present on the response ATLAS receives, which is where the entry price comes from. |
| **OCO protective exit (`POST /api/v3/order/oco`)** | **UNIT TESTED** | **Which OCO endpoint this account and testnet accept.** Binance also exposes a newer `/api/v3/orderList/oco` with a different parameter shape; `OCO_ENDPOINT` in `atlas/execution/broker.py` is the single place that choice is made. Also unproven: whether the stop and target prices satisfy the exchange's ordering rule (`take-profit > last > stop` for a SELL) at the moment of placement. |
| `myTrades` fill ingestion | UNIT TESTED | The exact response shape, and whether `clientOrderId` is present on every trade record. ATLAS treats a trade without one as an orphan. |
| Reconciliation against `openOrders` | UNIT TESTED | How an OCO's two legs appear in `openOrders`, and what remains after one fills. |
| Restart recovery | INTEGRATION TESTED | Recovery against real exchange state after a real restart. |
| Kill switch, audit chain, sizing, backtest, promotion, monitoring | UNIT TESTED | Nothing exchange-dependent. These are the parts least likely to move on contact. |

## Known unresolved, needing a decision

- **Stop-limit price equals the trigger price.** A `STOP_LOSS_LIMIT` whose limit sits
  exactly at its trigger frequently does not fill when price gaps through it, which
  leaves the position open and unprotected — the condition EXEC-02 exists to prevent.
  A buffer would fix it, but choosing one is a risk-policy decision (how much slippage
  to accept in exchange for certainty of exit), so ATLAS has not invented a number.

## How to advance a row

Run the preflight sequence in `docs/RUNBOOK.md` on a host with outbound access to
`testnet.binance.vision`, then record the observed response here with the date. A row
moves to TESTNET VERIFIED only with a real response behind it.
