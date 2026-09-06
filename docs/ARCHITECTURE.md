# Architecture

## Two planes

ATLAS is split by *authority*, not by layer. The split is the design.

```
┌─ ADVISORY PLANE ──────────────┐      ┌─ CONTROL PLANE ─────────────────┐
│  research/    strategy gen    │      │  risk/        sizing, limits    │
│  (LLM)        backtest req    │─────▶│  execution/   orders, brackets  │
│               read metrics    │ data │  monitor/     retirement rules  │
│               retire request  │ only │  killswitch/  account halt      │
│                               │      │  db/          persistence       │
│  NO exchange credentials      │      │  HOLDS credentials              │
│  READ-ONLY db role            │      │  SOLE WRITER                    │
└───────────────────────────────┘      └─────────────────────────────────┘
```

Everything crossing left to right is a **proposal**. Nothing crosses as a command.
The one exception is retirement, which is always honoured because it only ever reduces
exposure (AI-04).

## Module map

| Module | Plane | Responsibility | Phase |
|---|---|---|---|
| `atlas.config` | control | Typed settings. No financial defaults. | 1 |
| `atlas.models` | shared | Domain types. Decimal money. | 1 |
| `atlas.errors` | shared | Exception hierarchy. | 1 |
| `atlas.killswitch` | control | Account-level halt. Fail-safe. | 1 |
| `atlas.audit` | control | Hash-chained append-only log. | 1 |
| `atlas.db` | control | Schema, connections, repositories. | 1 |
| `atlas.data` | control | Binance klines, validation, cache. | 2 |
| `atlas.strategy` | control | Spec schema, indicators, evaluator. | 3 |
| `atlas.backtest` | control | Event-driven engine, costs, statistics. | 4 |
| `atlas.verify` | control | Second-engine comparison. | 5 |
| `atlas.selection` | control | Backtest-statistic gates. | 5 |
| `atlas.risk` | control | Sizing, limits, exchange filters, feasibility. | 6 |
| `atlas.research` | **advisory** | LLM generation loop, restricted tool surface. | 7 |
| `atlas.incubation` | control | Zero-capital tracking, divergence. | 8 |
| `atlas.promotion` | control | Gates, correlation, human approval. | 8 |
| `atlas.execution` | control | Binance orders, brackets, reconciliation. | 9 |
| `atlas.monitor` | control | Equity band, rolling stats, retirement. | 10 |
| `atlas.notify` | control | Outbound alerts. No inbound control. | 11 |
| `atlas.ops` | control | Dashboard, health, runbook tooling. | 11 |

## Data flow

```
Binance klines ──▶ validate ──▶ cache (content-hashed)
                                   │
                                   ├──▶ backtest ──▶ verify ──▶ selection gates
                                   │                                  │
LLM ──▶ spec (data) ──▶ schema ────┘                                  ▼
                                                              incubation (0 capital)
                                                                      │
                                                                      ▼
                                                          promotion (human approval)
                                                                      │
live klines ──▶ validate ──▶ evaluator ──▶ risk engine ──▶ execution ─┘
                                              │                │
                                     kill switch check     reconcile
                                                                │
                                                    monitor ──▶ retirement
```

## Invariants

1. Only closed candles produce signals (DATA-02).
2. A position never exists without its protective stop (EXEC-02).
3. The kill switch is checked immediately before every transmission (EXEC-06).
4. Exchange state is truth; local state is repaired to match (EXEC-04).
5. Retirement is one-way; no programmatic reactivation (MON-06).
6. Under-minimum positions are rejected, never rounded up (§6).
7. Money is `Decimal` everywhere (ADR-003).
8. The advisory plane holds no credential and writes no execution state.

## Persistence

SQLite/WAL behind a repository layer (ADR-001). The execution process is the sole
writer; the research plane connects read-only. Restart recovery replays from persisted
state and then reconciles against the exchange.
