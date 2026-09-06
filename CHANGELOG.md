# Changelog

All notable changes to ATLAS V2. Format follows Keep a Changelog; this project
versions by implementation phase rather than semver until Phase 11.

## [Unreleased]

### End-to-end integration
- Pipeline test driving one strategy through every stage: research → generation →
  validation → backtest → verification → selection → incubation → human
  promotion → live execution → monitoring → automatic retirement
- Promotion now checks lifecycle position before evidence quality, so a
  never-incubated strategy reports the real problem instead of weak evidence
- Tests that an armed kill switch halts the pipeline mid-flight, that unrecorded
  exposure stops everything, and that no credential-shaped string is committed

### Phase 11 — Operations
- Telegram alerting, outbound only: no command handler, polling loop or webhook
  receiver exists, and a test asserts none is added
- Every alert names its exchange environment, so a testnet alert cannot be read
  as live
- Failed alert delivery never raises — an unreachable notifier must not stop
  trading or retirement
- System health checks mapping data staleness and API error rate to their
  kill-switch triggers (DATA-05, KILL-02)
- Control-panel data: strategy listing, lifecycle funnel and survival rate

### Phase 10 — Monitoring and automatic retirement
- The source's three rules implemented with ATLAS parameters: equity-curve band
  breach, rolling win rate, rolling profit factor (MON-01..03)
- Two ATLAS additions: consecutive-loss suspension and signal-starvation alerting
  (MON-04, MON-05)
- Band sigma scales with sqrt(n), so the band does not tighten artificially as
  trades accumulate
- Any single rule retires — no quorum, weighting or averaging (MON-07)
- Retirement is one-way with no programmatic reactivation anywhere (MON-06)
- Regression test replaying a run-up → decay → false-recovery → collapse curve,
  asserting automatic retirement with no human consulted

### Phase 9 — Execution
- Signed Binance spot broker; testnet default, live requires an explicit choice
  and refuses to build an HTTP client implicitly (EXEC-01)
- Bracketed entry: stop placed with the entry, entry reversed if the stop is
  rejected, and a distinct error when the reversal also fails (EXEC-02)
- Deterministic client order IDs hashed from the decision, so a replayed signal
  collides with the original order instead of doubling up (EXEC-03)
- Reconciliation repairs local state from the exchange and arms the kill switch
  on unrecorded exposure or balance drift (EXEC-04, EXEC-05)
- Kill switch checked immediately before every entry transmission; protective
  orders still permitted while armed (EXEC-06, KILL-03)
- Retryable vs terminal rejection classification (EXEC-09)
- Order intent logged before the network call and the result after (EXEC-10)

### Phase 8 — Incubation and promotion
- Zero-capital incubation tracker resolving paper signals pessimistically, the
  same rule the backtest uses (INC-01..03)
- Backtest-vs-live divergence scoring: profit-factor retention and drawdown
  multiple, all reasons reported together (INC-04, INC-05)
- Pearson correlation gate against the live book, which is what makes the
  concurrency limit mean anything (PROM-02)
- Promotion requires explicit human approval and a named approver, refuses any
  strategy not currently INCUBATING, and records an immutable evidence snapshot
  (PROM-01, PROM-04)
- Reduced risk multiplier for a newly promoted strategy's first trades (PROM-03)

### Phase 7 — Research loop (first LLM contact)
- Advisory tool surface as an explicit allowlist; control-plane verbs named in a
  companion denylist so a regression is a failing test (AI-02)
- Static tests: no research module names a credential field, imports an
  execution module, or calls `eval`/`exec`/`compile` (AI-01, AI-02, AI-06)
- Read-only database role for the advisory plane, enforced by SQLite (AI-03)
- `ResearchLoop`: generate → validate → backtest → verify → gate → persist, whose
  terminal state is VERIFIED and never LIVE (AI-05)
- Every pass audited with model id, prompt hash and output hash (AI-07)
- Anthropic SDK is an optional extra; the control plane imports and runs without
  it (AI-08)
- Generation prompt kept deliberately short, per the source's own observation that
  more instruction produced worse results

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
