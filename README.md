# ATLAS V2

A **strategy factory** for Binance spot, in Python.

ATLAS is not a trading strategy. It manufactures, tests, incubates, deploys, monitors
and retires trading strategies — on the premise that markets are non-stationary and no
strategy is permanent. Its competence is measured by pipeline throughput and shutdown
reliability, not by the quality of any one strategy.

The architecture is derived from a primary-source analysis of DaviddTech's
"I Made My INSANELY Profitable AI Trading Bot Even Better", ported off TradingView and
Pine Script into self-hosted Python, and off Bybit futures onto Binance spot.
See [docs/SPECIFICATION.md](docs/SPECIFICATION.md) — the governing specification.

## The load-bearing constraint

**The AI never touches funds.** It researches, generates, evaluates and can *retire*
strategies. It cannot place, size, amend or authorise an order, promote a strategy, or
disarm the kill switch.

This is enforced by **process and credential separation**, not by prompt text:

- the research process never receives Binance API credentials (AI-01)
- its database role is read-only on execution tables (AI-03)
- the LLM tool surface contains no order, sizing, promotion or kill-switch function (AI-02)
- AI authority is one-way toward safety: it can stop a strategy, never start one (AI-04)

A prompt instruction not to trade is a request. A process without a key cannot trade.

## Pipeline

```
research → generation → schema validation → backtest → independent verification
        → selection gates → incubation (zero capital) → promotion (human approval)
        → live execution → position management → monitoring → automatic retirement
```

Gates reject terminally. Phases 1–6 contain no LLM and no network write path.

## Status

Evidence tiers are distinct and are not interchangeable:

| Tier | Meaning |
|---|---|
| **UNIT TESTED** | Passes in-process tests. Says nothing about the exchange. |
| **INTEGRATION TESTED** | Assembled components exercised together against scripted fixtures. |
| **TESTNET VERIFIED** | A real request/response with Binance Testnet succeeded. |
| **LIVE VERIFIED** | Exercised against Binance mainnet with real funds. |

| Component | Highest tier reached |
|---|---|
| Safety primitives (kill switch, audit chain, persistence) | INTEGRATION TESTED |
| Market data fetch / validate / cache | UNIT TESTED |
| Strategy spec, indicators, evaluator | UNIT TESTED |
| Backtest engine + independent verifier | UNIT TESTED |
| Selection gates | UNIT TESTED |
| Risk engine, sizing, exchange filters | UNIT TESTED |
| Research loop + AI containment | UNIT TESTED |
| Incubation, divergence, promotion | INTEGRATION TESTED |
| Broker, brackets, idempotency, reconciliation | INTEGRATION TESTED |
| Fill ingestion, ledger, positions | INTEGRATION TESTED |
| Monitoring, retirement, supervisor | INTEGRATION TESTED |
| Runtime service, scheduler, recovery, CLI | INTEGRATION TESTED |
| Heartbeat and health reporting | INTEGRATION TESTED |
| Deployment (Docker, systemd) | IMPLEMENTED — artefacts static-checked, image never built |
| Economic inputs (fees, slippage, filters) | **ASSUMPTIONS ONLY** — see `atlas.economics` |
| **Anything requiring Binance** | **NOT TESTED — see below** |

**Nothing in ATLAS has ever contacted a Binance endpoint.** No TESTNET VERIFIED or
LIVE VERIFIED component exists. The development environment's egress policy returns
`403` at the tunnel for both `testnet.binance.vision:443` and `api.binance.com:443`,
so every exchange interaction to date has been against a scripted stub.

565 tests, 88% coverage, ruff and mypy strict clean, GitHub Actions green.
Verify with `./scripts/ci-local.sh`, which runs the exact steps and command forms CI uses.

**No real-money trading.** No exchange credentials exist in this project, the account is
not funded, `ATLAS_EXCHANGE_ENV` defaults to `testnet`, and `live` is refused unless
`ATLAS_ENV=production`. See [docs/SECURITY.md](docs/SECURITY.md) and the pre-live
checklist in [docs/RUNBOOK.md](docs/RUNBOOK.md).

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env      # no financial parameter has a default; all must be set
./scripts/ci-local.sh     # lint, format, types, tests - exactly as CI runs them

atlas preflight           # verify exchange reachability and environment
atlas run --once          # a single trading cycle
atlas run                 # continuous, at ATLAS_TICK_INTERVAL_SECONDS
atlas killswitch status
atlas audit verify
```

`preflight` checks connectivity with an unsigned `/time` call before anything is
signed, so an unreachable exchange is distinguishable from a bad credential.

## Documentation

| Document | Purpose |
|---|---|
| [SPECIFICATION.md](docs/SPECIFICATION.md) | Governing spec. Every requirement, with provenance. |
| [ARCHITECTURE.md](docs/ARCHITECTURE.md) | Module map, planes, data flow. |
| [DECISIONS.md](docs/DECISIONS.md) | ADR log. |
| [SECURITY.md](docs/SECURITY.md) | Credential handling, containment, threat model. |
| [CONFIGURATION.md](docs/CONFIGURATION.md) | Every setting and its meaning. |
| [TRACEABILITY.md](docs/TRACEABILITY.md) | Requirement → implementation → test. |
| [RUNBOOK.md](docs/RUNBOOK.md) | Operations, incidents, kill-switch procedure. |

## Provenance discipline

Every requirement is tagged **Demonstrated / Stated / Inference / ATLAS / Unknown**.
Nothing is attributed to the source video that the video does not contain. All risk,
sizing and threshold policy is tagged **ATLAS** — it is our decision, not David's,
because the video supplies none.

## Disclaimer

Research software. Not financial advice. No profit is claimed or implied. The source
itself states that 99% of backtested strategies fail in live trading.
