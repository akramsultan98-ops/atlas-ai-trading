"""Look-ahead guard (BT-02).

A single engine's look-ahead bug produces beautiful, consistent, wrong results that no
amount of re-running catches. This wrapper makes the bug loud: any read of a bar the
engine has not yet reached raises immediately.
"""

from __future__ import annotations

from collections.abc import Sequence

from atlas.data.models import Kline
from atlas.errors import SafetyError


class LookaheadError(SafetyError):
    """Code attempted to read a bar that has not happened yet."""


class GuardedBars(Sequence[Kline]):
    """A bar sequence that refuses reads beyond the current index.

    `advance_to(i)` moves the frontier. Reads of index > frontier raise.
    """

    def __init__(self, bars: Sequence[Kline]) -> None:
        self._bars = bars
        self._frontier = -1

    def advance_to(self, index: int) -> None:
        self._frontier = index

    @property
    def frontier(self) -> int:
        return self._frontier

    def __len__(self) -> int:
        return len(self._bars)

    def __getitem__(self, index: int | slice) -> Kline:  # type: ignore[override]
        # Slicing would hand out a window the guard cannot police, so it is refused
        # outright rather than silently returning unguarded bars.
        if isinstance(index, slice):
            raise LookaheadError("slicing a guarded series is not permitted")
        resolved = index if index >= 0 else len(self._bars) + index
        if resolved > self._frontier:
            raise LookaheadError(
                f"look-ahead: read bar {resolved} while the engine is at bar "
                f"{self._frontier}. A decision at bar i may read only bars <= i (BT-01)."
            )
        return self._bars[resolved]
