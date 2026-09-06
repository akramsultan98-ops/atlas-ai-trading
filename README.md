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

| Phase | Scope | State |
|---|---|---|
| 0 | Repository, toolchain, CI | ✅ complete |
| 1 | Safety primitives — config, models, kill switch, audit, persistence | ✅ complete |
| 2 | Market data | ✅ complete |
| 3 | Strategy representation | ✅ complete |
| 4 | Backtest engine | ✅ complete |
| 5 | Verification and selection | ✅ complete |
| 6 | Risk and sizing | ✅ complete |
| 7 | Research loop | ✅ complete |
| 8 | Incubation and promotion | ✅ complete |
| 9 | Execution | ✅ complete (testnet only; no credentials exist) |
| 10 | Monitoring and retirement | ✅ complete |
| 11 | Operations | ✅ complete |
| 12 | Runtime orchestration | ✅ complete |

All twelve phases are implemented. **421 tests, ruff clean, mypy strict clean.**

**No real-money trading.** No exchange credentials exist in this project, the account is
not funded, and `ATLAS_EXCHANGE_ENV` defaults to `testnet` (which `Settings` refuses to
change outside `env=production`). The pre-live checklist is in
[docs/RUNBOOK.md](docs/RUNBOOK.md); it is not to be exercised until a full system audit
and explicit approval. See [docs/SECURITY.md](docs/SECURITY.md).

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env      # no financial parameter has a default; all must be set
pytest
```

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
