"""Bingo card generation (75-ball US bingo).

A card is a 5x5 grid. Columns are B/I/N/G/O, each drawing its 5 numbers from a
fixed range without repetition; the center cell (column N, row 2) is a FREE
space. Generation is fully deterministic from a ``seed`` via
:mod:`engine.rng`, so the same seed always yields the same card — the game
server hands a player a card and the on-chain plugin can regenerate the exact
same card to verify a BINGO claim.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Tuple

from .rng import DeterministicRNG, derive_seed

# FREE center sentinel. 0 is never a valid bingo number (1..75), so it is an
# unambiguous marker. (The React mockup used -1; convert at the UI boundary.)
FREE = 0

COLUMN_LETTERS: Tuple[str, ...] = ("B", "I", "N", "G", "O")
GRID_SIZE = 5
CENTER = (2, 2)  # (row, col) of the FREE space

# Inclusive number range for each column index 0..4.
COLUMN_RANGES: Tuple[Tuple[int, int], ...] = (
    (1, 15),    # B
    (16, 30),   # I
    (31, 45),   # N
    (46, 60),   # G
    (61, 75),   # O
)


def column_for_number(number: int) -> int:
    """Return the column index (0..4) a number belongs to."""
    if not 1 <= number <= 75:
        raise ValueError(f"number out of range 1..75: {number}")
    return (number - 1) // 15


def letter_for_number(number: int) -> str:
    return COLUMN_LETTERS[column_for_number(number)]


@dataclass(frozen=True)
class Card:
    """An immutable bingo card.

    ``columns[c]`` is the 5-number tuple for column ``c`` (top to bottom); the
    center of column N holds :data:`FREE`.
    """

    columns: Tuple[Tuple[int, ...], ...]

    def cell(self, row: int, col: int) -> int:
        return self.columns[col][row]

    def rows(self) -> List[List[int]]:
        """Row-major 5x5 view (row 0 first), for rendering/serialization."""
        return [[self.columns[c][r] for c in range(GRID_SIZE)] for r in range(GRID_SIZE)]

    def numbers(self) -> List[int]:
        """All 24 playable numbers on the card (excludes FREE)."""
        return [n for col in self.columns for n in col if n != FREE]

    def contains(self, number: int) -> bool:
        return any(number in col for col in self.columns)

    def to_grid(self, free_value: int = FREE) -> List[List[int]]:
        """Row-major grid, optionally re-mapping FREE (e.g. -1 for the UI)."""
        grid = self.rows()
        if free_value != FREE:
            r, c = CENTER
            grid[r][c] = free_value
        return grid


def generate_card(seed: bytes, index: int = 0) -> Card:
    """Deterministically generate a card from ``seed``.

    ``index`` lets one seed produce many distinct cards (a player buying 4 cards
    gets indices 0..3), each independent but reproducible.
    """
    card_seed = derive_seed(seed, b"card", index.to_bytes(4, "big"))
    rng = DeterministicRNG(card_seed)
    columns: List[Tuple[int, ...]] = []
    for col, (lo, hi) in enumerate(COLUMN_RANGES):
        pool = list(range(lo, hi + 1))
        picks = rng.shuffle(pool)[:GRID_SIZE]
        if col == CENTER[1]:
            picks[CENTER[0]] = FREE  # punch out the free center
        columns.append(tuple(picks))
    return Card(columns=tuple(columns))


def generate_cards(seed: bytes, count: int) -> List[Card]:
    """Generate ``count`` distinct cards for one player/round."""
    if count < 1:
        raise ValueError("count must be >= 1")
    return [generate_card(seed, i) for i in range(count)]


def validate_card(card: Card) -> bool:
    """Structural integrity check: correct shape, ranges, uniqueness, free center."""
    if len(card.columns) != GRID_SIZE:
        return False
    for col, (lo, hi) in enumerate(COLUMN_RANGES):
        column = card.columns[col]
        if len(column) != GRID_SIZE:
            return False
        for row, value in enumerate(column):
            if (row, col) == CENTER:
                if value != FREE:
                    return False
                continue
            if not lo <= value <= hi:
                return False
        playable = [v for r, v in enumerate(column) if (r, col) != CENTER]
        if len(set(playable)) != len(playable):
            return False
    return True
