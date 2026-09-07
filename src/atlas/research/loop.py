"""Research loop (AI-05, AI-07, pipeline stages 1-5).

David runs generation on a 15-minute schedule [08:55]. This is the same shape: each
pass generates one candidate and drives it through every gate. Nothing here can commit
capital — the loop's terminal state is a persisted CANDIDATE or VERIFIED strategy, and
promotion to live is a separate, human-gated step (PROM-01).

The loop never raises on a bad candidate. A malformed specification, a failed backtest
and a rejected gate are all ordinary outcomes recorded as audit events; the factory's
job is throughput, and 99% of candidates are expected to fail [22:36-22:47].
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from atlas.audit import AuditLog
from atlas.backtest.costs import DEFAULT_COSTS, CostModel
from atlas.backtest.engine import run_backtest
from atlas.data.models import KlineSeries
from atlas.db.engine import Database
from atlas.factory.provenance import config_hash
from atlas.factory.store import OUT_OF_SAMPLE, PRIMARY, FactoryStore
from atlas.models import AuditEventType, StrategyStatus, utcnow
from atlas.research.generator import (
    GenerationRecord,
    SpecClient,
    hash_text,
    validate_generated_spec,
)
from atlas.research.prompts import SYSTEM_PROMPT, build_user_prompt
from atlas.risk.sizing import ExchangeFilters, SizingPolicy
from atlas.selection.gates import SelectionThresholds, evaluate_gates
from atlas.strategy.evaluator import Signal, evaluate
from atlas.strategy.indicators import INDICATOR_NAMES
from atlas.strategy.registry import StrategyRegistry
from atlas.verify.compare import compare
from atlas.verify.vector_engine import run_verifier

RESEARCH_ACTOR = "research"


class Stage(StrEnum):
    GENERATION = "GENERATION"
    SCHEMA = "SCHEMA"
    BACKTEST = "BACKTEST"
    VERIFICATION = "VERIFICATION"
    SELECTION = "SELECTION"
    ACCEPTED = "ACCEPTED"


@dataclass(frozen=True)
class CandidateOutcome:
    """What happened to one candidate on one pass."""

    run_id: str
    stage: Stage
    accepted: bool
    reason: str
    strategy_id: str | None = None
    spec_hash: str | None = None
    engine_defect_suspected: bool = False
    at: datetime | None = None


class ResearchLoop:
    """One pass = generate -> validate -> backtest -> verify -> gate -> persist."""

    def __init__(
        self,
        db: Database,
        audit: AuditLog,
        client: SpecClient,
        *,
        policy: SizingPolicy,
        filters: ExchangeFilters | None = None,
        thresholds: SelectionThresholds | None = None,
        costs: CostModel = DEFAULT_COSTS,
        starting_equity: Decimal = Decimal(100),
        in_sample_fraction: float = 0.7,
    ) -> None:
        self._db = db
        self._audit = audit
        self._client = client
        self._registry = StrategyRegistry(db)
        self._store = FactoryStore(db)
        self._policy = policy
        self._filters = filters or ExchangeFilters()
        self._thresholds = thresholds or SelectionThresholds()
        self._costs = costs
        self._equity = starting_equity
        self._in_sample_fraction = in_sample_fraction
        self._config_hash = config_hash(
            policy=self._policy,
            filters=self._filters,
            costs=self._costs,
            starting_equity=self._equity,
        )

    def run_once(self, series: KlineSeries, variant: int = 0) -> CandidateOutcome:
        run_id = str(uuid.uuid4())
        symbol, timeframe = series.symbol, series.timeframe
        user_prompt = build_user_prompt(symbol, str(timeframe), sorted(INDICATOR_NAMES), variant)
        prompt_hash = hash_text(SYSTEM_PROMPT + user_prompt)

        # --- stage 1: generation ------------------------------------------------
        try:
            spec = self._client.generate(SYSTEM_PROMPT, user_prompt)
        except Exception as exc:
            # A failing model degrades throughput, never correctness (AI-08).
            return self._record(
                run_id,
                Stage.GENERATION,
                False,
                f"generation failed: {exc}",
                GenerationRecord(self._client.model_id, prompt_hash, "", False, str(exc)),
            )

        spec_hash = spec.content_hash()
        generation = GenerationRecord(
            self._client.model_id, prompt_hash, spec_hash, True, "generated"
        )

        # --- stage 2: schema and intent ----------------------------------------
        ok, reason = validate_generated_spec(spec, symbol, timeframe)
        if not ok:
            return self._record(run_id, Stage.SCHEMA, False, reason, generation)

        in_sample, out_of_sample = series.split_chronological(self._in_sample_fraction)

        # The candidate is persisted before it is judged. A rejected candidate with its
        # evidence on file is analysable; one discarded at the point of rejection tells
        # nobody why 99% of candidates fail, which is the number the factory exists to
        # move. Registration is idempotent on the spec hash, so re-proposing the same
        # rules re-uses the same strategy rather than forking its history.
        strategy_id = self._registry.register(spec)

        # --- stage 3: primary backtest -----------------------------------------
        primary = run_backtest(
            spec,
            in_sample,
            policy=self._policy,
            filters=self._filters,
            starting_equity=self._equity,
            costs=self._costs,
        )

        # --- stage 4: independent verification ---------------------------------
        verifier = run_verifier(
            spec,
            in_sample,
            policy=self._policy,
            filters=self._filters,
            starting_equity=self._equity,
            costs=self._costs,
        )
        verification = compare(primary, verifier)

        primary_id = self._store.record_backtest(
            strategy_id, primary, in_sample, engine=PRIMARY, config_hash=self._config_hash
        )
        verifier_id = self._store.record_verifier_run(
            strategy_id,
            verifier,
            in_sample,
            cost_model=primary.cost_model,
            config_hash=self._config_hash,
        )
        self._store.record_verification(
            strategy_id,
            verification,
            primary_backtest_id=primary_id,
            verifier_backtest_id=verifier_id,
        )

        if not verification.passed:
            # Left CANDIDATE, not REJECTED: a verification mismatch accuses the engines,
            # not the strategy, and rejecting the candidate would quietly discard the
            # evidence of a possible engine defect.
            return self._record(
                run_id,
                Stage.VERIFICATION,
                False,
                "; ".join(verification.reasons),
                generation,
                strategy_id=strategy_id,
                spec_hash=spec_hash,
                engine_defect=verification.engine_defect_suspected,
            )

        # --- stage 5: selection gates ------------------------------------------
        oos = run_backtest(
            spec,
            out_of_sample,
            policy=self._policy,
            filters=self._filters,
            starting_equity=self._equity,
            costs=self._costs,
        )
        signals = evaluate(spec, in_sample.bars)
        median_stop = _median_stop_distance(signals)
        self._store.record_backtest(
            strategy_id, oos, out_of_sample, engine=OUT_OF_SAMPLE, config_hash=self._config_hash
        )
        selection = evaluate_gates(
            primary.stats,
            out_of_sample=oos.stats,
            median_stop_distance=median_stop,
            thresholds=self._thresholds,
        )
        self._store.record_selection(strategy_id, selection)
        if not selection.passed:
            self._registry.set_status(strategy_id, StrategyStatus.REJECTED)
            return self._record(
                run_id,
                Stage.SELECTION,
                False,
                "; ".join(g.detail for g in selection.failures),
                generation,
                strategy_id=strategy_id,
                spec_hash=spec_hash,
            )

        # --- accepted: VERIFIED, awaiting an operator to begin incubation -------
        # Not INCUBATING: starting the observation clock is a deliberate act with a
        # recorded start time (INC-01), never a side effect of a batch run.
        self._registry.set_status(strategy_id, StrategyStatus.VERIFIED)
        return self._record(
            run_id,
            Stage.ACCEPTED,
            True,
            "passed every gate",
            generation,
            strategy_id=strategy_id,
            spec_hash=spec_hash,
        )

    def run_batch(self, series: KlineSeries, passes: int) -> list[CandidateOutcome]:
        """Run several passes. One bad candidate never stops the batch."""
        return [self.run_once(series, variant=i) for i in range(passes)]

    def _record(
        self,
        run_id: str,
        stage: Stage,
        accepted: bool,
        reason: str,
        generation: GenerationRecord,
        *,
        strategy_id: str | None = None,
        spec_hash: str | None = None,
        engine_defect: bool = False,
    ) -> CandidateOutcome:
        payload: dict[str, object] = {
            "run_id": run_id,
            "stage": str(stage),
            "accepted": accepted,
            "reason": reason,
            "strategy_id": strategy_id,
            "spec_hash": spec_hash,
            "engine_defect_suspected": engine_defect,
            **generation.as_dict(),
        }
        self._audit.append(AuditEventType.AI_ACTION, payload, RESEARCH_ACTOR)
        return CandidateOutcome(
            run_id=run_id,
            stage=stage,
            accepted=accepted,
            reason=reason,
            strategy_id=strategy_id,
            spec_hash=spec_hash,
            engine_defect_suspected=engine_defect,
            at=utcnow(),
        )


def _median_stop_distance(signals: list[Signal]) -> Decimal | None:
    if not signals:
        return None
    distances = sorted(s.stop_distance_fraction for s in signals)
    middle = len(distances) // 2
    if len(distances) % 2 == 1:
        return distances[middle]
    return (distances[middle - 1] + distances[middle]) / 2
