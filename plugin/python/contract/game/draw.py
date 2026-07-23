"""Deterministic ball draw for provably-fair Bingo.

The draw is a permutation of all 75 balls derived from ``seed`` via
:mod:`engine.rng`. The server commits to ``hash(seed)`` before the round and
reveals ``seed`` after; anyone can then recompute the exact same draw order and
confirm no ball was manipulated.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List

from .card import letter_for_number
from .rng import DeterministicRNG, derive_seed

TOTAL_BALLS = 75


@dataclass(frozen=True)
class Ball:
    number: int   # 1..75
    letter: str   # B/I/N/G/O

    def __str__(self) -> str:
        return f"{self.letter}{self.number}"


def ball(number: int) -> Ball:
    return Ball(number=number, letter=letter_for_number(number))


def draw_order(seed: bytes) -> List[int]:
    """Full deterministic permutation of balls 1..75 for this round."""
    draw_seed = derive_seed(seed, b"draw")
    rng = DeterministicRNG(draw_seed)
    return rng.shuffle(list(range(1, TOTAL_BALLS + 1)))


def draw_sequence(seed: bytes) -> List[Ball]:
    """Draw order as :class:`Ball` objects (with letters)."""
    return [ball(n) for n in draw_order(seed)]


def drawn_after(seed: bytes, count: int) -> List[int]:
    """The first ``count`` numbers drawn (0..75)."""
    if not 0 <= count <= TOTAL_BALLS:
        raise ValueError(f"count must be 0..{TOTAL_BALLS}")
    return draw_order(seed)[:count]
