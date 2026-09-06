-- ATLAS V2 canonical schema.
--
-- ADR-001 SQLite/WAL, single writer (the execution process); research plane read-only.
-- ADR-002 STRICT tables, foreign keys ON, CHECK constraints on enums and quantities.
--         The database is the last line of defence for state integrity; an invariant
--         expressed only in Python is one refactor away from being unenforced.
-- ADR-003 Money and quantities are TEXT holding a Decimal. Never REAL — REAL is a float.
--
-- All timestamps are ISO-8601 UTC strings.

PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------- audit (AI-07)

CREATE TABLE IF NOT EXISTS audit_events (
    seq         INTEGER PRIMARY KEY,
    event_type  TEXT NOT NULL,
    payload     TEXT NOT NULL,               -- JSON
    actor       TEXT NOT NULL,
    at          TEXT NOT NULL,
    prev_hash   TEXT NOT NULL CHECK (length(prev_hash) = 64),
    event_hash  TEXT NOT NULL UNIQUE CHECK (length(event_hash) = 64)
) STRICT;

CREATE INDEX IF NOT EXISTS idx_audit_at   ON audit_events(at);
CREATE INDEX IF NOT EXISTS idx_audit_type ON audit_events(event_type, at);

-- ------------------------------------------------------- kill switch (KILL-01..06)

CREATE TABLE IF NOT EXISTS kill_switch_state (
    id       INTEGER PRIMARY KEY CHECK (id = 1),   -- single row
    state    TEXT NOT NULL CHECK (state IN ('ARMED', 'DISARMED')),
    trigger  TEXT,
    reason   TEXT NOT NULL,
    actor    TEXT NOT NULL,
    at       TEXT NOT NULL
) STRICT;

-- ------------------------------------------------------------ strategies (STRAT-06)

CREATE TABLE IF NOT EXISTS strategy_specs (
    spec_hash  TEXT PRIMARY KEY CHECK (length(spec_hash) = 64),
    payload    TEXT NOT NULL,                -- JSON, immutable
    created_at TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS strategies (
    id            TEXT PRIMARY KEY,
    spec_hash     TEXT NOT NULL REFERENCES strategy_specs(spec_hash),
    symbol        TEXT NOT NULL,
    timeframe     TEXT NOT NULL,
    status        TEXT NOT NULL CHECK (status IN (
                      'CANDIDATE','VERIFIED','INCUBATING','PROMOTED',
                      'LIVE','SUSPENDED','RETIRED','REJECTED')),
    created_at    TEXT NOT NULL,
    status_at     TEXT NOT NULL,
    retired_at    TEXT,
    retire_reason TEXT,
    CHECK (status != 'RETIRED' OR retired_at IS NOT NULL)
) STRICT;

CREATE INDEX IF NOT EXISTS idx_strategies_status ON strategies(status);

-- ------------------------------------------------------------- backtests (BT-08)

CREATE TABLE IF NOT EXISTS backtests (
    id             TEXT PRIMARY KEY,
    strategy_id    TEXT NOT NULL REFERENCES strategies(id),
    engine         TEXT NOT NULL,            -- 'primary' | 'verifier'
    engine_version TEXT NOT NULL,
    data_hash      TEXT NOT NULL,
    cost_model     TEXT NOT NULL,            -- JSON: fees, slippage
    window_start   TEXT NOT NULL,
    window_end     TEXT NOT NULL,
    stats          TEXT NOT NULL,            -- JSON
    created_at     TEXT NOT NULL
) STRICT;

CREATE INDEX IF NOT EXISTS idx_backtests_strategy ON backtests(strategy_id, engine);

-- ---------------------------------------------------------- verification (VER-01..03)

CREATE TABLE IF NOT EXISTS verifications (
    id                  TEXT PRIMARY KEY,
    strategy_id         TEXT NOT NULL REFERENCES strategies(id),
    primary_backtest_id TEXT NOT NULL REFERENCES backtests(id),
    verifier_backtest_id TEXT NOT NULL REFERENCES backtests(id),
    passed              INTEGER NOT NULL CHECK (passed IN (0, 1)),
    detail              TEXT NOT NULL,       -- JSON: deltas vs tolerance
    created_at          TEXT NOT NULL
) STRICT;

-- ---------------------------------------------------------- selection (SEL-01..07)

CREATE TABLE IF NOT EXISTS selection_results (
    id          TEXT PRIMARY KEY,
    strategy_id TEXT NOT NULL REFERENCES strategies(id),
    passed      INTEGER NOT NULL CHECK (passed IN (0, 1)),
    gates       TEXT NOT NULL,               -- JSON: per-gate outcome
    created_at  TEXT NOT NULL
) STRICT;

-- ---------------------------------------------------------- incubation (INC-01..05)

CREATE TABLE IF NOT EXISTS incubation_signals (
    id           TEXT PRIMARY KEY,
    strategy_id  TEXT NOT NULL REFERENCES strategies(id),
    signal_at    TEXT NOT NULL,
    side         TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
    entry_price  TEXT NOT NULL,
    stop_price   TEXT NOT NULL,
    target_price TEXT NOT NULL,
    exit_at      TEXT,
    exit_price   TEXT,
    outcome      TEXT CHECK (outcome IN ('WIN', 'LOSS', 'OPEN')),
    return_pct   TEXT
) STRICT;

CREATE INDEX IF NOT EXISTS idx_incubation_strategy ON incubation_signals(strategy_id, signal_at);

-- ---------------------------------------------------------- promotion (PROM-01..04)

CREATE TABLE IF NOT EXISTS promotions (
    id            TEXT PRIMARY KEY,
    strategy_id   TEXT NOT NULL REFERENCES strategies(id),
    approved_by   TEXT NOT NULL,             -- human identity (PROM-01)
    approved_at   TEXT NOT NULL,
    evidence      TEXT NOT NULL,             -- JSON snapshot, immutable (PROM-04)
    initial_risk_multiplier TEXT NOT NULL    -- PROM-03
) STRICT;

-- ------------------------------------------------------------- orders (EXEC-01..10)

CREATE TABLE IF NOT EXISTS orders (
    id                TEXT PRIMARY KEY,
    client_order_id   TEXT NOT NULL UNIQUE,  -- deterministic (EXEC-03)
    strategy_id       TEXT NOT NULL REFERENCES strategies(id),
    exchange_order_id TEXT,
    symbol            TEXT NOT NULL,
    side              TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
    order_type        TEXT NOT NULL,
    role              TEXT NOT NULL CHECK (role IN ('ENTRY', 'STOP', 'TARGET')),
    quantity          TEXT NOT NULL,
    price             TEXT,
    status            TEXT NOT NULL,
    exchange_env      TEXT NOT NULL CHECK (exchange_env IN ('testnet', 'live')),
    -- The position this order belongs to. NULL until the entry fills, because an
    -- order is recorded before it is sent (an order that vanishes mid-flight must
    -- still leave a local record, or reconciliation reads it as unrecorded
    -- exposure). Set on both the entry and its protective orders, so an exit fill
    -- can close the position it actually belongs to.
    position_id       TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
) STRICT;

CREATE INDEX IF NOT EXISTS idx_orders_strategy ON orders(strategy_id, created_at);
CREATE INDEX IF NOT EXISTS idx_orders_status   ON orders(status);
CREATE INDEX IF NOT EXISTS idx_orders_position ON orders(position_id);

CREATE TABLE IF NOT EXISTS fills (
    id          TEXT PRIMARY KEY,
    order_id    TEXT NOT NULL REFERENCES orders(id),
    quantity    TEXT NOT NULL,
    price       TEXT NOT NULL,
    fee         TEXT NOT NULL,
    fee_asset   TEXT NOT NULL,
    filled_at   TEXT NOT NULL,
    exchange_trade_id TEXT
) STRICT;

CREATE INDEX IF NOT EXISTS idx_fills_order ON fills(order_id);

CREATE TABLE IF NOT EXISTS positions (
    id           TEXT PRIMARY KEY,
    strategy_id  TEXT NOT NULL REFERENCES strategies(id),
    symbol       TEXT NOT NULL,
    side         TEXT NOT NULL CHECK (side IN ('LONG', 'SHORT')),
    quantity     TEXT NOT NULL,
    entry_price  TEXT NOT NULL,
    stop_price   TEXT NOT NULL,
    target_price TEXT NOT NULL,
    opened_at    TEXT NOT NULL,
    closed_at    TEXT,
    exit_price   TEXT,
    realised_pnl TEXT
) STRICT;

CREATE INDEX IF NOT EXISTS idx_positions_open ON positions(strategy_id, closed_at);

-- ------------------------------------------------------------------- equity

CREATE TABLE IF NOT EXISTS equity_snapshots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    at          TEXT NOT NULL,
    equity      TEXT NOT NULL,
    free_cash   TEXT NOT NULL,
    deployed    TEXT NOT NULL,
    peak_equity TEXT NOT NULL,
    source      TEXT NOT NULL
) STRICT;

CREATE INDEX IF NOT EXISTS idx_equity_at ON equity_snapshots(at);

-- ------------------------------------------------------------- heartbeat (OPS)

-- Single row. Written every tick so an external health check can tell a running
-- process from a hung one without parsing logs.
CREATE TABLE IF NOT EXISTS heartbeat (
    id                INTEGER PRIMARY KEY CHECK (id = 1),
    at                TEXT NOT NULL,
    tick_count        INTEGER NOT NULL,
    last_error        TEXT,
    exchange_env      TEXT NOT NULL,
    exchange_reachable INTEGER NOT NULL CHECK (exchange_reachable IN (0, 1))
) STRICT;

-- ------------------------------------------------------------------- meta

CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
) STRICT;
