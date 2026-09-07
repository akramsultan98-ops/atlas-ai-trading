"""Persistence for factory evidence (BT-07, VER-04, SEL-08).

The `backtests`, `verifications` and `selection_results` tables were declared in the
schema from the beginning and never written to by any code. The research loop computed
all three results, made its accept/reject decision, and discarded the evidence — which
meant a promotion could cite nothing, an operator could inspect nothing, and INC-04/05
had no stored backtest to compare incubation against.

Every record carries the provenance needed to reproduce it: the spec hash, the data
hash, the engine version, the cost model and the configuration hash. A result whose
inputs cannot be named is not evidence.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from atlas.backtest.engine import BacktestResult
from atlas.data.models import KlineSeries
from atlas.db.engine import Database
from atlas.models import utcnow
from atlas.selection.gates import SelectionOutcome
from atlas.verify.compare import VerificationOutcome
from atlas.verify.vector_engine import VerifierResult

PRIMARY = "primary"
VERIFIER = "verifier"
# The held-out window SEL-06 is decided on. Stored separately so the two windows are
# never mistaken for one another when reading evidence back.
OUT_OF_SAMPLE = "out_of_sample"


@dataclass(frozen=True)
class StoredBacktest:
    """One recorded run, with everything needed to say what produced it."""

    id: str
    strategy_id: str
    engine: str
    engine_version: str
    data_hash: str
    config_hash: str
    cost_model: dict[str, str]
    window_start: str
    window_end: str
    stats: dict[str, Any]
    created_at: str

    @property
    def trade_count(self) -> int:
        return int(self.stats.get("trade_count", 0))

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "strategy_id": self.strategy_id,
            "engine": self.engine,
            "engine_version": self.engine_version,
            "data_hash": self.data_hash,
            "config_hash": self.config_hash,
            "cost_model": self.cost_model,
            "window_start": self.window_start,
            "window_end": self.window_end,
            "stats": self.stats,
            "created_at": self.created_at,
        }


def _window(series: KlineSeries) -> tuple[str, str]:
    if not series.bars:
        return "", ""
    return series.bars[0].open_time.isoformat(), series.bars[-1].close_time.isoformat()


class FactoryStore:
    """Reads and writes the evidence a promotion has to be able to cite."""

    def __init__(self, db: Database) -> None:
        self._db = db

    # ------------------------------------------------------------------ writes

    def record_backtest(
        self,
        strategy_id: str,
        result: BacktestResult,
        series: KlineSeries,
        *,
        engine: str = PRIMARY,
        config_hash: str = "",
    ) -> str:
        start, end = _window(series)
        backtest_id = str(uuid.uuid4())
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO backtests(id, strategy_id, engine, engine_version, data_hash, "
                "config_hash, cost_model, window_start, window_end, stats, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    backtest_id,
                    strategy_id,
                    engine,
                    result.engine_version,
                    result.data_hash,
                    config_hash,
                    json.dumps(result.cost_model, sort_keys=True),
                    start,
                    end,
                    json.dumps(result.stats.as_dict(), sort_keys=True),
                    utcnow().isoformat(),
                ),
            )
        return backtest_id

    def record_verifier_run(
        self,
        strategy_id: str,
        result: VerifierResult,
        series: KlineSeries,
        *,
        cost_model: dict[str, str],
        config_hash: str = "",
    ) -> str:
        """Store the independent engine's numbers.

        The verifier computes only what it needs to contradict the primary engine -
        trade count, net return, final equity - and deliberately shares no code with
        it, so it has no `BacktestStats` to hand over. Recording the three numbers it
        does produce is what makes a past disagreement re-examinable.
        """
        start, end = _window(series)
        backtest_id = str(uuid.uuid4())
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO backtests(id, strategy_id, engine, engine_version, data_hash, "
                "config_hash, cost_model, window_start, window_end, stats, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    backtest_id,
                    strategy_id,
                    VERIFIER,
                    result.engine_version,
                    series.content_hash(),
                    config_hash,
                    json.dumps(cost_model, sort_keys=True),
                    start,
                    end,
                    json.dumps(
                        {
                            "trade_count": result.trade_count,
                            "net_return": str(result.net_return),
                            "final_equity": str(result.final_equity),
                        },
                        sort_keys=True,
                    ),
                    utcnow().isoformat(),
                ),
            )
        return backtest_id

    def record_verification(
        self,
        strategy_id: str,
        verification: VerificationOutcome,
        *,
        primary_backtest_id: str,
        verifier_backtest_id: str,
    ) -> str:
        verification_id = str(uuid.uuid4())
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO verifications(id, strategy_id, primary_backtest_id, "
                "verifier_backtest_id, passed, detail, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    verification_id,
                    strategy_id,
                    primary_backtest_id,
                    verifier_backtest_id,
                    1 if verification.passed else 0,
                    json.dumps(
                        verification.as_dict(),
                        sort_keys=True,
                    ),
                    utcnow().isoformat(),
                ),
            )
        return verification_id

    def record_selection(self, strategy_id: str, outcome: SelectionOutcome) -> str:
        selection_id = str(uuid.uuid4())
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO selection_results(id, strategy_id, passed, gates, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    selection_id,
                    strategy_id,
                    1 if outcome.passed else 0,
                    json.dumps(outcome.as_dict(), sort_keys=True),
                    utcnow().isoformat(),
                ),
            )
        return selection_id

    # ------------------------------------------------------------------- reads

    def backtests_for(self, strategy_id: str) -> list[StoredBacktest]:
        rows = self._db.connection.execute(
            "SELECT * FROM backtests WHERE strategy_id = ? ORDER BY created_at", (strategy_id,)
        ).fetchall()
        return [
            StoredBacktest(
                id=str(r["id"]),
                strategy_id=str(r["strategy_id"]),
                engine=str(r["engine"]),
                engine_version=str(r["engine_version"]),
                data_hash=str(r["data_hash"]),
                config_hash=str(r["config_hash"]),
                cost_model=json.loads(str(r["cost_model"])),
                window_start=str(r["window_start"]),
                window_end=str(r["window_end"]),
                stats=json.loads(str(r["stats"])),
                created_at=str(r["created_at"]),
            )
            for r in rows
        ]

    def primary_backtest(self, strategy_id: str) -> StoredBacktest | None:
        """The most recent primary run. INC-04/05 compare incubation against this."""
        runs = [b for b in self.backtests_for(strategy_id) if b.engine == PRIMARY]
        return runs[-1] if runs else None

    def verification_for(self, strategy_id: str) -> dict[str, Any] | None:
        row = self._db.connection.execute(
            "SELECT * FROM verifications WHERE strategy_id = ? ORDER BY created_at DESC LIMIT 1",
            (strategy_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "id": str(row["id"]),
            "passed": bool(row["passed"]),
            "primary_backtest_id": str(row["primary_backtest_id"]),
            "verifier_backtest_id": str(row["verifier_backtest_id"]),
            "detail": json.loads(str(row["detail"])),
            "created_at": str(row["created_at"]),
        }

    def selection_for(self, strategy_id: str) -> dict[str, Any] | None:
        row = self._db.connection.execute(
            "SELECT * FROM selection_results WHERE strategy_id = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (strategy_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "id": str(row["id"]),
            "passed": bool(row["passed"]),
            "gates": json.loads(str(row["gates"])),
            "created_at": str(row["created_at"]),
        }

    def backtest_stats_decimal(self, strategy_id: str) -> dict[str, Decimal] | None:
        """Stored primary stats as Decimals, for gates that compare against them."""
        stored = self.primary_backtest(strategy_id)
        if stored is None:
            return None
        out: dict[str, Decimal] = {}
        for key, value in stored.stats.items():
            if isinstance(value, str):
                try:
                    out[key] = Decimal(value)
                except (ArithmeticError, ValueError):
                    continue
        return out


__all__ = ["OUT_OF_SAMPLE", "PRIMARY", "VERIFIER", "FactoryStore", "StoredBacktest"]
