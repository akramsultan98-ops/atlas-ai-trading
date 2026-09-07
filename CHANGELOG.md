# Changelog

All notable changes to ATLAS V2. Format follows Keep a Changelog; this project
versions by implementation phase rather than semver until Phase 11.

## [Unreleased]

### Analysis quality: evidence-driven, regime-aware, calibrated, able to abstain
`atlas.intel` — the LLM is an analyst and hypothesis generator here, never the decision
maker. No single model response can produce a trade.

- **The seven parts stay separate** (`intel/evidence.py`): observed fact, source,
  model interpretation, market-impact hypothesis, confidence, horizon. A record with an
  interpretation but no fact is refused at construction — a model talking about nothing
  is not analysis. Records carry no decision field at all: what ATLAS does is decided
  downstream by deterministic code from many records.
- **Events, not headlines** (`intel/events.py`): dedup is by content, and corroboration
  counts *distinct sources*, so one wire story republished five times is one
  observation. Sources that disagree are recorded as conflicting and the claim is
  suppressed, never resolved in favour of the newest or loudest. An event observed
  before it was published is refused: one of the two clocks is wrong.
- **Regime is measured, not asserted** (`intel/regime.py`): trend by directional
  efficiency (a market that ends where it started after a large round trip is *ranging*,
  not flat-trending) and volatility by range ratio. Risk-on/risk-off needs cross-asset
  data, so it reports `UNAVAILABLE` rather than inferring it from one pair's direction.
  Too little data reports `UNCLEAR`, which is not the same as calm. Every tick decision
  now carries the measured regime — replacing the `NOT_IMPLEMENTED` placeholder.
- **Confluence, not averaging** (`intel/confluence.py`): evidence is grouped into
  independent families and agreement is required *across* them. A hypothesis supported
  only by narrative groups cannot trade however confident the model is — conviction is
  not evidence. One opposing family abstains rather than being outvoted.
- **Abstention is a first-class outcome**: `NO_TRADE`, `INSUFFICIENT_EVIDENCE`,
  `CONFLICTING_EVIDENCE`, `STALE_INTELLIGENCE`, `UNCONFIRMED_EVENT`, `REGIME_UNCLEAR`.
- **Confidence is four separate quantities** (`intel/calibration.py`): model-asserted,
  evidence-counted, historically measured, and calibrated probability. A stated 0.9 is
  `UNCALIBRATED` until at least 30 resolved outcomes exist in that bucket, and
  `calibrated_probability` is then `None` — no fallback number, because any number
  there would be read as a probability. A bucket claiming 0.9 that resolves at 0.67 is
  reported as poorly calibrated, with both numbers shown.
- **Accuracy that cannot flatter itself** (`intel/metrics.py`): abstentions are never
  counted as correct and abstention rate is reported beside accuracy, never folded in.
  Directional accuracy, precision, recall, F1, false-positive rate, win rate, profit
  factor, expectancy, max drawdown — each broken out by regime, event category and
  confidence bucket. `is_reportable` is false when nothing has resolved, so no accuracy
  claim can be made from an empty set.
- **Leakage guards** (`intel/splits.py`): five chronological phases with no overlap;
  overlapping windows are refused because a held-out set that overlaps research was
  already seen. `assert_no_lookahead` raises rather than filtering — silently dropping
  future data hides that the caller built a leaking dataset. Only OUT_OF_SAMPLE,
  FORWARD_INCUBATION and LIVE are marked as evidence of skill.
- **Token control** (`intel/triage.py`): deterministic → shallow → deep, reached only
  when a case is both uncertain and consequential. An already-seen event does not buy a
  second opinion. Budget exhaustion degrades the tier and never fails the tick, and with
  no provider configured everything returns DETERMINISTIC (AI-08).

**No accuracy claim is made.** There is no out-of-sample evidence yet, so the framework
reports `reportable: false` rather than a number.

### The research plane is now runnable from configuration
- `ATLAS_RESEARCH_PROVIDER` / `ATLAS_RESEARCH_API_KEY` / `ATLAS_RESEARCH_MODEL`. The
  research credential is separate from the exchange credential and neither plane holds
  the other's: `for_research_plane()` strips the Binance keys, and the new
  `for_execution_plane()` strips the research key. A model provider reachable with an
  exchange key would make prompt injection an execution path.
- `research/provider.py` builds the configured `SpecClient`. That Protocol was already
  the provider interface, so this is a registry, not a new architecture. A missing
  credential fails at the moment generation is attempted, naming the variable to set —
  never at import, because the control plane must run with no provider configured (AI-08).
- `atlas research run` executes the full pipeline on **real** candles and the symbol's
  **real** `exchangeInfo` filters, so SEL-07 feasibility is decided against the actual
  minimum notional rather than the assumed $5. Both endpoints are public and unsigned:
  the research process authenticates to nothing.
- `atlas research provider` reports the credential state and asserts no exchange key is
  present in the research plane.

### The strategy factory: evidence, and gates that cannot pass by omission
Two defects of the same kind as the unfed ledger — a declared structure nothing wrote,
and a check that passed by staying silent.

- **`backtests`, `verifications` and `selection_results` were never written by any
  code.** The research loop computed all three, decided on them, and discarded the
  evidence. A promotion could therefore cite nothing, an operator could inspect nothing,
  and INC-04/05 had no stored backtest to compare incubation against. `FactoryStore` now
  records every run with the provenance needed to reproduce it: spec hash, data hash,
  engine version, cost model, window and a new configuration hash. Candidates are
  persisted before they are judged, so a *rejected* candidate keeps its evidence — which
  is what makes "99% of candidates fail" an analysable claim rather than a slogan.
- **A selection gate with no evidence was omitted from the result entirely**, so a
  candidate with no out-of-sample window passed selection having never been tested out
  of sample. Gates now return `PASS` / `FAIL` / `UNDEFINED_POLICY`, and
  `UNDEFINED_POLICY` blocks exactly as `FAIL` does. Missing out-of-sample data
  (SEL-06) and an unmeasurable stop distance (SEL-07) are the two cases.
- **INC-01's 60 days was satisfiable in one second.** Elapsed incubation was measured
  from the earliest paper signal, and paper signals carry the timestamp of the bar that
  produced them — so replaying a year of history reported a year of "incubation" the
  moment it loaded. A new `incubation_runs` row records when forward observation
  actually began; the start is written once and never moved, and a strategy with no
  recorded start has been observed for zero days whatever signals exist against it.
- `config_hash` covers the risk policy, exchange filters, cost model and starting
  equity. Two results are only comparable when it matches; without it a strategy can
  look better than another purely because it was measured under different rules.
- `atlas factory {candidates,show,backtests,verification,gates,incubation,eligibility}`
  is read-only. `atlas factory incubate --confirm` is the single lifecycle action, and
  starts the observation clock at zero capital (INC-03).
- **There is deliberately no `promote` CLI command.** Promotion is what puts capital
  behind a strategy; the only path to it requires an explicit human approver in code
  (PROM-01). A shell verb would put live trading one typo away from an inspection.

No threshold was invented: every SEL/INC/PROM value used is the one already recorded in
`docs/SPECIFICATION.md`. `UNDEFINED_POLICY` reports the absence of *evidence*, and stops
the candidate rather than guessing.

### Why the tick did what it did
A tick reporting `entries=0` was indistinguishable from a tick that had declined the
market, one that never looked because nothing was deployed, and one that wanted to
trade and was refused by risk. Those three have different remedies and the audit trail
recorded the same thing for all of them: nothing.

- Every configured symbol now produces exactly one `SymbolDecision` per tick with a
  machine-readable `DecisionOutcome`. The outcomes separate three families that were
  previously one silence: *absence* (`NO_STRATEGY`, `SPEC_MISSING` — nothing was
  evaluated), *unobservable market* (`NO_MARKET_DATA`, `DATA_INVALID`, `DATA_STALE`),
  and *judgement* (`NO_SIGNAL`, `POSITION_ALREADY_OPEN`, `RISK_REJECTED`,
  `ORDER_REJECTED`, `ENTERED`).
- The entry loop iterates the configured universe rather than only live strategies. A
  symbol with nothing deployed against it was invisible to that loop — precisely the
  case that most needs reporting. A live strategy on an *unconfigured* symbol is now
  reported too; it previously fetched no data and took no entry, silently.
- `evaluator.explain` reports the per-condition truth table with the operand values a
  rule was actually decided on. ATLAS strategies are boolean rules, not scored
  (STRAT-02), so there is no score or threshold number to report; the truth table is
  the honest and more useful equivalent.
- Decisions are written to the hash-chained audit log as `TICK_DECISION`, so the
  reasoning survives the process and can be read back after the fact.
- `atlas run --once --explain` prints the decision for every symbol; `--json` emits it
  machine-readably. `atlas decisions` replays the last recorded decision per symbol
  from the audit trail.
- `cli.py` had **no test coverage at all**. The decision surface is now tested, taking
  the module from 0% to 45%.

There is no regime filter in ATLAS. Rather than omit the step or invent a verdict for
it, every decision carries `regime: NOT_IMPLEMENTED`, so an operator looking for that
stage learns it does not exist instead of wondering whether it rejected the trade.

No risk parameter, threshold, strategy rule or safety gate changed.

### The exit side of a trade
- **No take-profit order was ever placed.** Specification line 57 puts stop *and target*
  enforcement in the control plane, STRAT-02/03 require a target rule on every spec, and
  BT-06 exits the backtest at either level -- but live execution placed only the stop. A
  live strategy therefore could never realise a winner at its target while its backtest
  did, a structural shortfall the monitor reads as decay and retires the strategy for.
  The exit is now a single OCO order list carrying both levels: two independent sell
  orders cannot both stand against one spot holding, because the first locks the base
  asset. A fill on either leg closes the position.
- **Protective prices were never snapped to the tick grid.** A price off the grid is
  rejected outright by `PRICE_FILTER`. Both levels are now rounded toward the entry,
  which can only reduce the risk the position was sized for and can only make the target
  easier to reach; rounding away from entry would let realised risk exceed the budget
  that authorised the trade.
- `docs/EVIDENCE.md` records what is actually known to work and how. Nothing in ATLAS
  has ever reached TESTNET VERIFIED; the OCO endpoint in particular has never touched a
  real exchange, and the register says so.

### The ledger had no producer
`Ledger.open_position` and `Ledger.close_position` had zero callers anywhere in
`src/`. The positions table was never written by production code, so the ledger was a
fully tested, entirely unfed data structure. Everything downstream of it was inert:

- `open_symbols` was always empty, so RISK-08 never bound and a strategy could
  re-enter the same symbol every tick
- deployed capital and marked equity read zero however much was actually at risk
- `realised_returns` stayed empty, so MON-01..06 could never retire anything
- every trade from `myTrades` was an orphan, because no order was on file
- reconciliation found ATLAS orders on the exchange with no local record and armed
  `RECONCILIATION_FAILURE` -- on the first restart after any order existed

`open_bracketed_position` now records the entry, the stop and any reversal in the
ledger, opens the position at the price the entry *filled* at
(`cummulativeQuoteQty / executedQty`, not the signalled bar close), and links every
order to it. Orders are written before they are sent: an order accepted by the
exchange whose response never arrives must still leave a local record, or
reconciliation reads it as unrecorded exposure.

`FillIngestor` closes a position when its protective order fills in full. Nothing else
observes this -- the exchange sends no notification and the order simply stops
appearing in `openOrders`. A partial fill closes nothing; the remainder is still held
and still protected.

A failed placement now records what it actually knows: `NOT_SENT` when the kill switch
refused it before any network call, `REJECTED` when the exchange answered terminally,
and left `SUBMITTED` when retries were exhausted against a transport fault -- that last
one may be live, and erasing it would hide a real order from reconciliation.

An entry that fills nothing no longer has a stop placed against it, and a reversed
entry closes its position rather than leaving a row that blocks the symbol forever.

`orders.position_id` is new; existing databases are migrated on open, since
`CREATE TABLE IF NOT EXISTS` leaves an older table alone.

### Pre-testnet integration fixes
Three requirements held in their components and failed in the assembled system.

- **RISK-05/RISK-06 — equity counted only quote cash.** An entry converts quote into
  base, so the account appeared to lose the whole position notional the instant a fill
  landed. Reproduced against the previous code: $70 cash plus a $30 open position was
  valued at $70, a 30% drawdown, arming `MAX_ACCOUNT_DD` on the first trade of a $100
  account. `AtlasService.account_state` now marks open positions from the ledger at the
  last close, and counts locked quote as well as free.
- **RISK-09 — the live path sized every symbol against hardcoded filters.**
  `ExchangeFilterCache` existed and was tested but was never constructed;
  `build_service` passed the assumed defaults (minNotional $5, step 1e-5) straight to
  the trading service. Filters are now supplied per symbol by a `SymbolFilterProvider`,
  the runtime wires the live cache, and `TradingService` refuses a fixed provider at
  construction. A symbol whose filters cannot be read is declined, never guessed at.
- **RISK-04 — the deployment cap never bound.** The tick passed `deployed=0`
  unconditionally, so a portfolio could deploy all of equity while the 75% limit
  reported itself satisfied. Deployment and free cash now come from the account
  snapshot and are carried forward within a tick, so a second entry is sized against
  the first.
- An account that cannot be fully valued (an open position with no price this tick) is
  neither compared against a loss limit nor persisted as a snapshot: entries are
  suspended and the switch is left alone, since the alternative manufactures a breach
  out of a market-data gap and biases every later drawdown comparison. An armed kill
  switch outranks a suspension in what the tick reports.
- `preflight` now fetches real filters per symbol, reports the $100 feasible stop band
  from the exchange's own minNotional, compares clock drift against the broker's
  `recvWindow`, and reports free/locked quote and open-order count.

### Deployment, monitoring, security and economic provenance
- Dockerfile (non-root, volume-backed state, HEALTHCHECK, no default `run`),
  compose file with bounded resources, hardened systemd unit
- Single-row heartbeat written on success and failure alike; `atlas health` exits
  0/1 for orchestrators, and an armed kill switch is not treated as unhealthy
- `atlas.economics`: ASSUMPTION / OBSERVED / FIXTURE provenance for every economic
  input, with `is_exchange_verified` false while any input remains assumed
- Security audit findings locked into tests: two-variable live gate, SecretStr
  credentials, no code execution, no shell, parameterised SQL, research-plane
  isolation, no withdrawal path

### Exchange layer wired (Phases B–G)
- `UrllibBrokerTransport`: signed Binance REST over the standard library, with
  bounded retries, terminal-vs-retryable classification and query-string redaction
  so signatures never reach a log or an exception
- Broker read methods for reconciliation and ingestion: `server_time`,
  `exchange_info`, `order_status`, `my_trades`
- `AtlasService` + `build_service`: constructs and connects every component and runs
  the operating loop; acquires and validates its own market data
- `FillIngestor`: the ledger's missing producer, resuming from the highest recorded
  trade id, idempotent on exchange trade id
- `atlas` console script with `preflight`, `run`, `killswitch`, `audit`
- Configuration for symbols, timeframe, tick interval, history depth, Telegram
- Fixed: `decimal.InvalidOperation` escaped ingestion's handler (it subclasses
  `ArithmeticError`, not `ValueError`), so one malformed price would abort a poll

### Fixed — CI had never passed
- `pythonpath = ["src", "."]` so cross-module fixture imports resolve under the bare
  `pytest` console script CI uses, not only under `python -m pytest`
- `scripts/ci-local.sh` runs the exact CI steps in the exact CI command form
- ADR-007 records the invocation trap
- README test count reconciled to the verified 462 across 27 modules, 92% coverage

### Order, fill and position ledger
- Writer for the `orders`, `fills` and `positions` tables, which had existed since
  Phase 1 with nothing populating them
- Fills are idempotent on the exchange trade id, since exchanges re-deliver trades
  on reconnect and on overlapping polled windows
- Position PnL and closed-trade returns feed the monitor (MON-01..04) and open
  symbols feed the per-symbol limit (RISK-08)
- Quantities summed in Python rather than SQL, so money never round-trips through
  SQLite's REAL (ADR-003)

### Phase 12 — Runtime orchestration
- Interval scheduler: a failing tick is recorded and the loop continues; only a
  sustained run of consecutive failures stops it
- Restart recovery in the runbook's order — kill switch, reconcile, then resume
  monitoring before entries (KILL-06, EXEC-04, EXEC-05)
- `TradingService.tick`: reconcile, portfolio limits, staleness, sized bracketed
  entries, then supervision — with retirement still running while halted
- Repository target guard script (`scripts/verify-repo-target.sh`)

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
