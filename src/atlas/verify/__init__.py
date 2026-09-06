"""Independent verification (VER-01..05)."""

from atlas.verify.compare import VerificationOutcome, compare
from atlas.verify.vector_engine import VERIFIER_VERSION, VerifierResult, run_verifier

__all__ = [
    "VERIFIER_VERSION",
    "VerificationOutcome",
    "VerifierResult",
    "compare",
    "run_verifier",
]
