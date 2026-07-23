"""BINGO marking, win-pattern detection and claim validation.

This is the arbiter of the game. The game server uses it live; the on-chain
plugin uses the SAME code inside ``deliver_tx`` to re-verify a claimed BINGO
before releasing the escrow — so an invalid claim can never be paid.
"""

from __future__ import annotations

from enum import Enum
from typing import FrozenSet, Iterable, List, Set, Tuple

from .card import CENTER, FREE, GRID_SIZE, Card
from .draw import draw_order

Coord = Tuple[int, int]  # (row, col)


def _row(r: int) -> FrozenSet[Coord]:
    return frozenset((r, c) for c in range(GRID_SIZE))


def _col(c: int) -> FrozenSet[Coord]:
    return frozenset((r, c) for r in range(GRID_SIZE))


class Pattern(str, Enum):
    """Winning shapes. ``LINE`` (any row/col/diagonal) is the default win."""
    ROW = "row"
    COLUMN = "column"
    DIAGONAL = "diagonal"
    LINE = "line"            # any single row, column or diagonal
    FOUR_CORNERS = "four_corners"
    FULL_HOUSE = "full_house"


_DIAG_MAIN = frozenset((i, i) for i in range(GRID_SIZE))
_DIAG_ANTI = frozenset((i, GRID_SIZE - 1 - i) for i in range(GRID_SIZE))
_FOUR_CORNERS = frozenset({(0, 0), (0, GRID_SIZE - 1), (GRID_SIZE - 1, 0), (GRID_SIZE - 1, GRID_SIZE - 1)})
_FULL_HOUSE = frozenset((r, c) for r in range(GRID_SIZE) for c in range(GRID_SIZE))

# Every individual line (5 rows + 5 cols + 2 diagonals).
LINES: Tuple[FrozenSet[Coord], ...] = tuple(
    [_row(r) for r in range(GRID_SIZE)]
    + [_col(c) for c in range(GRID_SIZE)]
    + [_DIAG_MAIN, _DIAG_ANTI]
)


def marked_cells(card: Card, drawn: Iterable[int]) -> Set[Coord]:
    """Coordinates that are daubed: the FREE center plus any drawn number."""
    drawn_set = set(drawn)
    cells: Set[Coord] = {CENTER}  # FREE is always marked
    for c in range(GRID_SIZE):
        for r in range(GRID_SIZE):
            value = card.columns[c][r]
            if value != FREE and value in drawn_set:
                cells.add((r, c))
    return cells


def _pattern_targets(pattern: Pattern) -> List[FrozenSet[Coord]]:
    """The set(s) of coords that, if fully marked, complete ``pattern``."""
    if pattern == Pattern.ROW:
        return [_row(r) for r in range(GRID_SIZE)]
    if pattern == Pattern.COLUMN:
        return [_col(c) for c in range(GRID_SIZE)]
    if pattern == Pattern.DIAGONAL:
        return [_DIAG_MAIN, _DIAG_ANTI]
    if pattern == Pattern.LINE:
        return list(LINES)
    if pattern == Pattern.FOUR_CORNERS:
        return [_FOUR_CORNERS]
    if pattern == Pattern.FULL_HOUSE:
        return [_FULL_HOUSE]
    raise ValueError(f"unknown pattern: {pattern}")


def has_bingo(card: Card, drawn: Iterable[int], pattern: Pattern = Pattern.LINE) -> bool:
    """True if ``card`` completes ``pattern`` given the ``drawn`` numbers."""
    cells = marked_cells(card, drawn)
    return any(target <= cells for target in _pattern_targets(pattern))


def completed_lines(card: Card, drawn: Iterable[int]) -> List[FrozenSet[Coord]]:
    """All individual lines currently completed (for progress/animation)."""
    cells = marked_cells(card, drawn)
    return [line for line in LINES if line <= cells]


def first_win_index(card: Card, order: List[int], pattern: Pattern = Pattern.LINE) -> int:
    """The number of balls that must be drawn (from ``order``) for ``card`` to
    first complete ``pattern``. Returns -1 if it never completes within the
    order. This is the fair tie-breaker for multiplayer ranking: the lowest
    index wins.
    """
    targets = _pattern_targets(pattern)
    cells: Set[Coord] = {CENTER}
    # index positions of the card's numbers within the draw order
    for i, number in enumerate(order, start=1):
        for c in range(GRID_SIZE):
            col = card.columns[c]
            for r in range(GRID_SIZE):
                if col[r] == number:
                    cells.add((r, c))
        if any(target <= cells for target in targets):
            return i
    return -1


def validate_claim(
    card: Card,
    seed: bytes,
    balls_drawn: int,
    pattern: Pattern = Pattern.LINE,
) -> bool:
    """Authoritative check used at settlement time.

    Re-derives the draw order from ``seed``, takes the first ``balls_drawn``
    numbers, and confirms ``card`` genuinely has ``pattern``. Because both seed
    and card are reproducible, this is what the on-chain plugin runs before
    paying out — the claimant cannot fake a win.
    """
    if balls_drawn < 0:
        raise ValueError("balls_drawn must be non-negative")
    order = draw_order(seed)[:balls_drawn]
    return has_bingo(card, order, pattern)
