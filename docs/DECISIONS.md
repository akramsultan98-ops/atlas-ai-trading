# Architecture Decision Record

Decisions taken beyond what the specification fixes. Each is reversible only by a new
ADR. Decisions inherited from the specification (§6, §7 thresholds, §11 deviations) are
recorded there, not duplicated here.

---

## ADR-001 — SQLite with WAL as the persistence engine

**Status:** accepted (Phase 1)

**Context.** The spec requires durable persistence for strategies, backtests, orders,
fills, equity and audit events, plus restart recovery. It does not name an engine.
ATLAS is a single-node, single-writer system managing one $100 account.

**Decision.** SQLite in WAL mode, accessed behind a thin repository layer.

**Rationale.** ACID without an external service; trivial backup (copy one file);
trivial restart recovery; no network dependency in the persistence path, which matters
because a database outage must never be able to orphan a live position. Write volume is
a few rows per trade — orders of magnitude below where SQLite's single-writer limit
matters.

**Consequences.** No concurrent multi-process writes; the execution process is the sole
writer. The research plane gets a **separate read-only connection** (AI-03). If ATLAS
ever needs multi-node deployment, the repository layer is the swap point for Postgres.

---

## ADR-002 — Foreign keys and strict typing enforced at the database level

**Status:** accepted (Phase 1)

**Decision.** `PRAGMA foreign_keys=ON`, `STRICT` tables, and `CHECK` constraints on
every enum-like column and every non-negative quantity.

**Rationale.** The database is the last line of defence for state integrity. A financial
invariant expressed only in Python is one refactor away from being unenforced. Constraints
that can be expressed in the schema are expressed in the schema.

---

## ADR-003 — Money as `Decimal`, stored as TEXT

**Status:** accepted (Phase 1)

**Decision.** All monetary and quantity values are `decimal.Decimal` in Python and
stored as TEXT in SQLite. Never `float`, at any layer.

**Rationale.** Binary floating point cannot represent decimal fractions exactly.
Accumulated error in position sizing or PnL is a correctness bug with financial
consequence. SQLite's `REAL` is a float, so TEXT with `Decimal` round-tripping is the
only lossless option; it sorts correctly when zero-padded, and we never sort on money
columns in a hot path.

---

## ADR-004 — Kill switch resolves disagreement toward ARMED

**Status:** accepted (Phase 1) — implements KILL-04, KILL-06

**Decision.** The kill switch persists to a file *and* the database. If the two
disagree, or either is unreadable, unparseable or missing, the effective state is
**ARMED**.

**Rationale.** The failure mode of a kill switch that fails open is unbounded loss; the
failure mode of one that fails closed is missed trades. These are not symmetric. Every
ambiguity resolves toward the survivable outcome.

**Consequences.** A corrupted state file halts trading until a human intervenes. This is
intended.

---

## ADR-005 — Hash-chained audit log

**Status:** accepted (Phase 1) — implements AI-07, EXEC-10

**Decision.** Audit events carry `prev_hash` and `event_hash`, forming a chain from a
genesis record. Verification walks the chain.

**Rationale.** The spec requires an append-only audit trail. "Append-only by convention"
is not a property, it is a hope. A hash chain makes silent modification or deletion of
historical events detectable, which is what an audit trail is for. Cost is one SHA-256
per event.

**Consequences.** Events cannot be edited or backfilled. Corrections are new events.

---

## ADR-006 — No financial parameter has a default value

**Status:** accepted (Phase 1)

**Decision.** `config.py` supplies defaults for operational settings (paths, log levels)
but **none** for risk percentage, position caps, loss limits, or drawdown limits. A
missing value is a startup error.

**Rationale.** A default risk parameter is a silent policy. If configuration is
misloaded, the correct behaviour is a loud failure, not trading at a plausible-looking
number nobody chose.

---

## ADR-007 — Verify against CI's command form, not a convenient one

**Status:** accepted

**Context.** CI failed on 15 consecutive runs while local runs reported green. The cause
was not the code: `python -m pytest` inserts the current working directory into
`sys.path`, while the bare `pytest` console script does not. Fourteen test modules import
shared fixtures as `from tests.test_backtest import ...`, which resolves only when the
repository root is importable. Local verification used `python -m pytest`; CI uses
`pytest`. Local was passing under a more permissive `sys.path` than CI ever had.

**Decision.** `pythonpath = ["src", "."]` in the pytest configuration, so the repository
root is on `sys.path` regardless of how pytest is invoked. `scripts/ci-local.sh` runs the
exact CI steps in the exact CI command form, and is the only local run that counts as
evidence CI will pass.

**Rationale.** The failure was invisible precisely because the local check was weaker
than the real one. Fixing the import path stops the error; running the same command form
stops the class of error. A green local run that used a different invocation than CI is
not evidence about CI.

**Consequences.** Claims about CI status must cite `scripts/ci-local.sh` or an actual
GitHub Actions run. A bare `pytest`/`python -m pytest` difference is now a configuration
detail rather than a behavioural one.
