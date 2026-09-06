# ATLAS V2 — Master Specification

Status: **APPROVED** — governing specification for this repository.
Source: DaviddTech, "I Made My INSANELY Profitable AI Trading Bot Even Better" (`f9GO0ZEaCmI`),
analysed from a 24:41 timestamped transcript. Bracketed timestamps cite that transcript.

## Provenance taxonomy

Every requirement carries exactly one provenance tag.

| Tag | Meaning |
|---|---|
| **Demonstrated** | Performed on screen in the video. |
| **Stated** | Said by David, not shown being done. |
| **Inference** | Follows necessarily from what is shown. |
| **ATLAS** | **Our decision.** The video is silent; we choose and own it. |
| **Unknown** | Not provided. Tracked in §12. |

> Nothing tagged Demonstrated or Stated may be invented. Where the video gives no
> answer, the requirement is tagged **ATLAS** and is our policy, not David's.

---

## §1 Thesis and scope

ATLAS V2 is **not a trading strategy**. It is a factory that manufactures, tests, and
retires trading strategies. The video demonstrates no entry or exit logic at all: the
generation prompt instructs the model to *research* indicators so output does not
converge on common setups [07:54–08:12]. The strategy is the loop's output.

The design premise, from the source: markets are non-stationary, no strategy is
permanent, and the operator's most important job is knowing when to switch a bot off
[18:30–18:37].

### Non-goals for V2

- **No leverage, margin, futures, or derivatives.** Spot only. *(ATLAS — deviates from
  the source, which backtests with leverage [17:30].)*
- **No discretionary override.** There is no "trade now" control.
- **No profitability claim.** The source states 99% of backtested strategies fail live
  [22:36–22:47]. ATLAS is built to survive that base rate.
- **No feedback loop from live results into generation.** The video demonstrates none;
  adding one invites overfitting to a handful of live samples. Deferred.

---

## §2 AI containment boundary

**Normative. Overrides every other section on conflict.**

Inherited from the source — in David's system the model never places an order — and
hardened from prompt discipline into a process and credential boundary.

| Plane | Contents |
|---|---|
| **Advisory** (AI permitted) | Research concepts; author strategy specs; request backtests; read results and live metrics; **request retirement of any strategy** (always honoured); draft reports. |
| **Control** (deterministic code only) | Sizing; order placement/amendment/cancellation; stop and target enforcement; exposure caps; promotion to live; reactivation; kill-switch arm/disarm. |

| ID | Requirement | Provenance |
|---|---|---|
| AI-01 | The research process MUST NOT hold Binance API credentials. Keys are readable only by the execution process, from a separate secret scope. | ATLAS |
| AI-02 | The LLM tool surface MUST NOT expose any function that places, sizes, amends or cancels an order, alters risk configuration, promotes a strategy, or disarms the kill switch. Absence of the tool is the enforcement, not prompt text. | ATLAS |
| AI-03 | The research process's database role MUST be read-only on all execution, order and risk tables. | ATLAS |
| AI-04 | AI authority is **one-way toward safety**: it MAY retire or disable a strategy, and MUST NOT be able to enable, resume or scale one up. | Stated [21:35–21:51] |
| AI-05 | Every AI-authored spec MUST pass schema validation, backtest, independent verification, selection gates and incubation before capital is committed. No path shortens this. | Demonstrated |
| AI-06 | LLM output MUST be treated as untrusted input: parsed as structured data against a strict schema, never executed as code, never interpolated into a query or an order. | ATLAS |
| AI-07 | Every AI action MUST be written to an append-only audit log with prompt hash, model ID and output hash. | ATLAS |
| AI-08 | Total loss of the AI layer MUST be survivable: live strategies keep trading, monitoring and retirement continue. Only candidate production stops. | ATLAS |

**Why credentials, not prompts:** a prompt instruction not to trade is a request. A
process that never receives an API key cannot trade regardless of what it is told,
what it infers, or what a compromised model attempts.

---

## §3 The pipeline

Ten stages. Gates (*) reject terminally for that spec version.

| # | Stage | Provenance |
|---|---|---|
| 1 | Generation — scheduled loop; LLM emits a spec as structured data | Demonstrated [08:55] |
| 2 | Schema validation * | Inference |
| 3 | Primary backtest | Demonstrated |
| 4 | Independent verification * — second engine reproduces the result | Demonstrated [11:00–11:08] |
| 5 | Selection gates * | Stated; thresholds ATLAS |
| 6 | Incubation * — zero capital, live data | Stated [12:27–12:36] |
| 7 | Promotion * — divergence check then **human approval** | ATLAS |
| 8 | Live execution — signal → risk engine → bracketed order | Demonstrated |
| 9 | Monitoring — equity band, rolling win rate, rolling profit factor | Stated |
| 10 | Retirement * — automatic, one-way, no discretion | Stated |

---

## §4 Data and strategy representation

| ID | Requirement | Provenance |
|---|---|---|
| DATA-01 | Binance klines are the sole price source for backtest, incubation and live. | ATLAS |
| DATA-02 | Only **closed** candles produce signals. An in-progress candle MUST NEVER be evaluated. | ATLAS |
| DATA-03 | Series MUST be validated before use: OHLC consistency, monotonic non-duplicated timestamps, no gaps beyond one interval. Failure rejects the series; it is NEVER interpolated. | Inference |
| DATA-04 | Backtest data MUST be cached locally and content-hashed so a backtest is reproducible. | ATLAS |
| DATA-05 | Live data staleness beyond 2× the bar interval halts new entries and alerts. | ATLAS |
| DATA-06 | Default research timeframe is `1h`, matching the source. Configurable per strategy. | Demonstrated [06:20] |
| STRAT-01 | A strategy is a **declarative specification, not code**. The LLM emits data; the engine interprets it. No generated code is ever executed. | ATLAS |
| STRAT-02 | A spec MUST declare: symbol, timeframe, sides, indicator set, entry conditions, exit conditions, stop rule, target rule. | Stated [08:18–08:26] |
| STRAT-03 | Stop and target MUST be defined in advance and computable at entry from data available at that bar's close. | Stated [08:20–08:26] |
| STRAT-04 | **Trailing stops are not representable.** The schema has no field for them. Rationale is execution latency [08:29–08:50]. | Stated |
| STRAT-05 | Evaluation MUST be a pure function of (spec, bars). | ATLAS |
| STRAT-06 | Specs are content-hash versioned and immutable. Editing produces a new strategy with fresh history. | ATLAS |
| STRAT-07 | Indicators come from a fixed, individually tested library. The LLM composes from it; it cannot introduce primitives. | ATLAS |

---

## §5 Backtest and independent verification

| ID | Requirement | Provenance |
|---|---|---|
| BT-01 | Event-driven, bar by bar. Decisions at bar `i` may read only bars `≤ i`. | ATLAS |
| BT-02 | An explicit look-ahead guard MUST raise on access to future data. A deliberately peeking strategy MUST fail loudly. | ATLAS |
| BT-03 | Fills execute at the **next** bar's open, never the signal bar's close. | ATLAS |
| BT-04 | Costs: 0.10% taker fee per side + 0.05% slippage per side = 0.30% round trip. | ATLAS |
| BT-05 | The backtest MUST apply the **same sizing, position cap and minimum-notional rules as live** (§6). | ATLAS |
| BT-06 | Stop and target checked intrabar against high/low. If both touch in one bar, resolve **pessimistically** — the stop fills. | ATLAS |
| BT-07 | Statistics: net return, profit factor, expectancy, max drawdown, win rate, trade count, avg bars held, buy-and-hold benchmark. | Stated [23:00–23:14] |
| BT-08 | Every run persists data hash, spec hash, engine version and cost assumptions. | ATLAS |
| VER-01 | A second, **independently implemented** engine MUST reproduce every candidate's result. | Demonstrated [11:00] |
| VER-02 | Tolerance: trade count ±2%, net return ±5% relative. Outside tolerance rejects. | ATLAS |
| VER-03 | A mismatch MUST also raise an engine-defect alert. | ATLAS |
| VER-04 | Out-of-sample split: chronological 70/30. Gates evaluated on both halves. | ATLAS |
| VER-05 | Minimum history: 2 years of 1h data, or full listed history if shorter and ≥ 1 year. | ATLAS |

**What VER buys:** a single engine's look-ahead bug produces beautiful, consistent,
wrong results that no amount of re-running catches. Independence is the value — the
verification engine MUST share no code with the primary.

---

## §6 Risk, sizing and the $100 constraint

**The video is silent here. All of §6 is an ATLAS decision.** The source's only sizing
reference is "percentage of portfolio" in passing [17:30].

```
R              = equity × RISK_PCT
stop_distance  = |entry − stop| / entry
notional_risk  = R / stop_distance
notional_cap   = equity × MAX_POSITION_PCT
notional       = min(notional_risk, notional_cap, free_cash)
qty            = floor_to_step(notional / entry, LOT_SIZE.stepSize)

REJECT if qty × entry < NOTIONAL.minNotional    # never round up to reach it
REJECT if qty < LOT_SIZE.minQty
REJECT if open_positions >= MAX_CONCURRENT
REJECT if deployed + notional > equity × MAX_DEPLOYED_PCT
```

| ID | Parameter | Value | Rationale |
|---|---|---|---|
| RISK-01 | RISK_PCT | 1.0% | $1.00/trade at $100. Ten losses cost 10%. Lower than a conventional 1.5–2% because the source's base rate is 99% failure. |
| RISK-02 | MAX_CONCURRENT | 3 | "Never run one single bot" [23:56]. Three is the ceiling before min notional bites. |
| RISK-03 | MAX_POSITION_PCT | 33.3% | equity ÷ MAX_CONCURRENT. |
| RISK-04 | MAX_DEPLOYED_PCT | 75% | 25% cash buffer for fees and slippage. |
| RISK-05 | DAILY_LOSS_LIMIT | 5% | Trips kill switch for the UTC day. |
| RISK-06 | MAX_ACCOUNT_DD | 20% | From peak equity. Trips kill switch until a human clears it. |
| RISK-07 | Leverage | 1× spot | No margin, no futures. Deviates from source. |
| RISK-08 | Per symbol | 1 position | No pyramiding, averaging down, or hedging. |
| RISK-09 | Exchange filters | live | `LOT_SIZE`, `NOTIONAL`, `PRICE_FILTER` from `exchangeInfo` at startup, refreshed daily. NEVER hardcoded. |
| RISK-10 | Quote asset | USDT | Single quote asset keeps equity accounting trivial. |

### Feasibility at $100

With RISK_PCT 1.0%, MAX_POSITION_PCT 33.3%, minNotional $5 (illustrative — RISK-09
requires the real value):

```
lower bound = RISK_PCT / MAX_POSITION_PCT = 3.0%   ← structural, fixed at any equity
upper bound = (equity × RISK_PCT) / minNotional     ← scales with equity

  $100 →  3.0% .. 20.0%        $500 → 3.0% .. 100%
  $250 →  3.0% .. 50.0%       $1000 → 3.0% .. 200%
```

- The **3% floor never moves**: it is the ratio of risk budget to position cap.
- The **20% ceiling is where $100 hurts**; it relaxes as equity grows.
- `SEL-07` rejects strategies outside the band so incubation is not wasted.
- Fees are material: 0.30% round trip against a 3% stop is 10% of the risk budget.

---

## §7 Selection, incubation, promotion

David names every criterion and supplies no number. Criteria are his; **thresholds are
ATLAS** and live in configuration so they can be recalibrated.

| ID | Gate | Threshold | Provenance |
|---|---|---|---|
| SEL-01 | Trade count | ≥ 100 | criterion Stated, value ATLAS |
| SEL-02 | Max drawdown | ≤ 25% | criterion Stated [11:41–11:54], value ATLAS |
| SEL-03 | Profit factor, net of costs | ≥ 1.30 | criterion Stated, value ATLAS |
| SEL-04 | Expectancy per trade | > 0 | criterion Stated, value ATLAS |
| SEL-05 | Beats buy-and-hold | required | Stated [23:00] |
| SEL-06 | Out-of-sample profit factor | ≥ 1.10 and ≥ 0.70 × in-sample | ATLAS |
| SEL-07 | Median stop distance | within live feasible band (§6) | ATLAS |
| INC-01 | Minimum incubation | 60 days | Stated "a couple of months" [12:27] |
| INC-02 | Minimum incubation trades | ≥ 30 | ATLAS |
| INC-03 | Capital during incubation | zero | Stated |
| INC-04 | Incubation profit factor | ≥ 0.70 × backtest PF | ATLAS |
| INC-05 | Incubation max drawdown | ≤ 1.5 × backtest max DD | ATLAS |
| PROM-01 | Human approval | required, explicit | ATLAS — stricter than source |
| PROM-02 | Portfolio correlation | ≤ 0.6 vs any live strategy | ATLAS |
| PROM-03 | Initial live allocation | 50% of RISK_PCT for first 20 trades | ATLAS |
| PROM-04 | Promotion record | immutable evidence snapshot | ATLAS |

**PROM-02 is what makes RISK-02 mean anything.** Three strategies that are all
long-BTC-momentum on the 1h are one strategy wearing three hats, and they draw down
together.

---

## §8 Execution

| ID | Requirement | Provenance |
|---|---|---|
| EXEC-01 | Binance spot only. Testnet and live behind one interface, selected by config, environment surfaced in every log line and alert. | ATLAS |
| EXEC-02 | Every entry MUST place its protective stop in the same operation. A position MUST NEVER exist unprotected; if the stop fails, the entry is immediately reversed. | Inference |
| EXEC-03 | Client order IDs are deterministic from `(strategy_id, signal_bar_ts, side)`. Replaying a signal MUST NOT create a second order. | ATLAS |
| EXEC-04 | Exchange state is the source of truth. Reconciliation at startup, after every fill, and on a fixed interval. | ATLAS |
| EXEC-05 | An unreconcilable divergence trips the kill switch rather than guessing. | ATLAS |
| EXEC-06 | The kill switch is checked immediately before every order transmission, not only at signal time. | ATLAS |
| EXEC-07 | API keys MUST be restricted to spot trading with withdrawals disabled, IP-allowlisted where possible. | Inference [13:49–14:20] |
| EXEC-08 | No trailing stop, no stop amendment after entry, no position scaling. | Stated [08:29] |
| EXEC-09 | Rejections classified retryable (rate limit, timeout) vs terminal (insufficient balance, filter violation). Terminal never retries. | ATLAS |
| EXEC-10 | All order intents, transmissions, responses and fills written to an append-only log before and after the network call. | ATLAS |

---

## §9 Monitoring and retirement

The three rules are the source's core contribution. **Parameters are ATLAS; the rules
are David's.** He lost a +2181% strategy because he had exactly these rules and
overrode them [17:56–18:03].

| ID | Rule | ATLAS parameters | Action |
|---|---|---|---|
| MON-01 | Equity-curve band break — the "ultimate stop" [21:12–21:33] | Expected equity from backtest per-trade return distribution; band = expected − 2σ; rolling 30-trade window | Retire |
| MON-02 | Rolling win rate [21:01–21:12] | 30-trade window; trigger below 0.60 × backtest win rate | Retire |
| MON-03 | Rolling profit factor [21:06–21:12] | 30-trade window; trigger below 1.00 | Retire |
| MON-04 | Consecutive losses | 8 — *ATLAS addition*, catches sharp regime breaks faster than a 30-trade window | Suspend |
| MON-05 | Signal starvation | No signal in 3× backtest mean inter-trade interval — *ATLAS addition* | Alert |
| MON-06 | Retirement is one-way | No new entries; open positions run to existing brackets; **no programmatic reactivation** — human only [21:35–21:51] | — |
| MON-07 | Retirement needs no quorum | Any single rule firing retires. Rules are not averaged, weighted or voted on. | — |

**MON-06 and MON-07 encode the actual lesson.** The failure was a present rule
overridden by its author because the strategy briefly recovered. A system that lets any
component — human, model, or weighted score — talk itself out of a shutdown reproduces
the loss rather than the safeguard.

### Kill switch

| ID | Requirement | Provenance |
|---|---|---|
| KILL-01 | Account-level halt, distinct from per-strategy retirement. The source has no equivalent. | ATLAS |
| KILL-02 | Triggers: RISK-05, RISK-06, EXEC-05, DATA-05, sustained API error rate, or manual. | ATLAS |
| KILL-03 | When armed: no new entries system-wide. Existing protective stops remain — cancelling them would leave positions naked. | ATLAS |
| KILL-04 | State persists in both a file and the database. Disagreement is itself a trigger, resolved toward armed. | ATLAS |
| KILL-05 | Disarm requires an explicit human action with a reason string. NO AI-reachable path exists. | ATLAS |
| KILL-06 | Fails safe: if state cannot be read, the system behaves as if armed. | ATLAS |

---

## §10 Implementation phases

| Phase | Scope | Discharges |
|---|---|---|
| 0 | Repository, toolchain, CI, docs | — |
| 1 | Safety primitives: config, models, kill switch, audit, schema | KILL-01..06, AI-07 |
| 2 | Market data | DATA-01..06 |
| 3 | Strategy representation | STRAT-01..07 |
| 4 | Primary backtest engine | BT-01..08 |
| 5 | Verification and selection | VER-01..05, SEL-01..07 |
| 6 | Risk and sizing | RISK-01..10, SEL-07, BT-05 |
| 7 | Research loop | AI-01..08 |
| 8 | Incubation and promotion | INC-01..05, PROM-01..04 |
| 9 | Execution | EXEC-01..10 |
| 10 | Monitoring and retirement | MON-01..07 |
| 11 | Operations | OPS, DATA-05, KILL-02 |

Phases 1–6 contain **no LLM and no network write path**. The entire control plane is
built and tested before an AI touches the system.

---

## §11 Deviations from the source

| Area | David | ATLAS V2 | Reason |
|---|---|---|---|
| Venue | Bybit, futures | Binance, spot | Target venue; spot removes liquidation risk at $100 |
| Leverage | Used in backtests | 1× only | Unproven auto-generated strategy + leverage + $100 = liquidation |
| Strategy format | Pine Script (code) | Declarative spec (data) | Executing LLM-written code would breach §2 |
| Backtest engine | Hosted (tradingkit) | Own engine + third-party verifier | Self-hosted; independence preserved |
| Verification | Manual paste into TradingView | Automatic dual-engine comparison | Same intent, no human step to skip |
| Execution path | TradingView alert → webhook → bridge | In-process risk engine → exchange | Risk controls must be independently enforceable |
| Promotion | His judgement | Gated, then human approval | Explicit approval beats implicit habit |
| Account halt | None demonstrated | Kill switch | Per-strategy retirement does not bound account loss |
| Sizing | Unspecified | §6 in full | The video supplies nothing; we must |

---

## §12 Open unknowns

| Unknown | Effect | Resolution |
|---|---|---|
| David's boilerplate [07:37–07:54] | Candidate quality differs from his. Pipeline unaffected. | His longer video, if it exists |
| His numeric thresholds | None — every §6/§7 threshold is ATLAS, in configuration | Recalibrate after Phase 7 throughput |
| Std-dev band parameters | MON-01's 2σ / 30-trade window is ours | Sensitivity-test in Phase 10 |
| Generation prompt text | Phase 7 prompt quality | Video description |
| Monitoring MCP identity | None — ATLAS implements monitoring natively | Not required |
| Live Binance minNotional | Shifts §6 upper bound; $5 is illustrative | RISK-09 reads at runtime |
| Binance fee tier | BT-04 assumes 0.10%/side | Confirm before live cutover |
