# Changelog

All notable changes to ATLAS V2. Format follows Keep a Changelog; this project
versions by implementation phase rather than semver until Phase 11.

## [Unreleased]

### Phase 6 — Risk engine completed
- Live `exchangeInfo` filter fetch and daily-refresh cache; a symbol missing any
  required filter raises rather than sizing against assumed values (RISK-09)
- Portfolio-level limits: daily loss and drawdown-from-peak, each mapped to its
  kill-switch trigger (RISK-05, RISK-06, KILL-02)
- Drawdown takes precedence over daily loss when both breach, so the more
  serious condition is the recorded reason
- One position per symbol (RISK-08)

### Phase 5 — Independent verification and selection
- Second backtest engine written independently of the primary: forward-scan
  rather than a bar state machine, indicators recomputed from first principles,
  no shared code (VER-01)
- Cross-engine comparison with relative tolerances; a mismatch rejects the
  candidate and raises a suspected engine defect (VER-02, VER-03)
- Selection gates SEL-01..07 with all thresholds in configuration, since the
  source supplies criteria but no numbers
- All gates evaluated rather than short-circuited, so rejection reasons stay
  analysable in aggregate

### Phase 4 — Backtest engine
- Event-driven engine: next-open fills (BT-03), pessimistic stop-vs-target
  resolution (BT-06), entry bar cannot also exit
- `GuardedBars` look-ahead guard that raises on any forward read (BT-02)
- Cost model: 0.10% fee + 0.05% slippage per side, slippage always against the
  trader (BT-04)
- Position sizing brought forward from Phase 6 so the backtest applies real live
  sizing rules rather than a toy sizer (BT-05); rejects rather than rounding up
  to reach minNotional
- Statistics incl. costed buy-and-hold benchmark (BT-07); provenance recorded on
  every run (BT-08)
- Feasible stop band verified against the specification table: 3.0%–20.0% at $100

### Phase 3 — Strategy representation
- Declarative `StrategySpec`: strategies are data, never executable code (STRAT-01)
- No trailing-stop field anywhere in the schema, making it unrepresentable (STRAT-04)
- Fixed indicator library — sma, ema, rsi, atr, true_range, rolling high/low,
  volume_sma — each tested against known values and against forward reads (STRAT-07)
- Pure `evaluate(spec, bars)` producing signals with stop and target fixed at
  signal time (STRAT-03, STRAT-05)
- Content-hash spec versioning and a registry whose RETIRED state is terminal
  (STRAT-06, MON-06)

### Phase 2 — Market data
- `Kline` / `KlineSeries` with Decimal prices and enforced OHLC consistency
- Binance kline client with injected transport; drops the in-progress bar (DATA-02)
- Series validation for gaps, duplicates and ordering — rejects, never interpolates
  (DATA-03)
- Content-hashed gzip cache with integrity verification on load (DATA-04)
- Staleness detection at 2x the bar interval (DATA-05)
- Chronological out-of-sample split (VER-04)

### Phase 1 — Safety primitives
- Typed configuration with no defaults for any financial parameter (ADR-006)
- Domain models with `Decimal` money types; `float` rejected at construction (ADR-003)
- Fail-safe kill switch: dual-store, disagreement and unreadability resolve to ARMED
  (KILL-01..06, ADR-004)
- Hash-chained append-only audit log with tamper detection (AI-07, ADR-005)
- SQLite/WAL persistence with STRICT tables, foreign keys and CHECK constraints
  (ADR-001, ADR-002)

### Phase 0 — Repository and toolchain
- Repository scaffold, packaging, ruff + mypy strict + pytest, GitHub Actions CI
- Governing specification committed (`docs/SPECIFICATION.md`)
- Architecture, security, configuration, traceability, runbook and ADR documentation
