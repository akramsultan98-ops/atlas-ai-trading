"""Promotion to live capital (PROM-01..04). Human approval required."""

from atlas.promotion.gate import (
    PromotionDecision,
    PromotionGate,
    PromotionRefused,
    PromotionThresholds,
    evaluate_promotion,
    pearson_correlation,
)

__all__ = [
    "PromotionDecision",
    "PromotionGate",
    "PromotionRefused",
    "PromotionThresholds",
    "evaluate_promotion",
    "pearson_correlation",
]
