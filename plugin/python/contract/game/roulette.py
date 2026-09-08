"""European roulette: spin derivation and bet settlement.

Single-zero wheel (0-36, no 00). The spin is derived from the revealed seed
the exact same way Bingo derives its draw order -- the plugin is the only
place this logic runs, so a re-derived spin from the same seed always agrees
with what was actually paid out on-chain; there is no separate "trust the
server" step.
"""

from __future__ import annotations

from typing import Optional

from .rng import DeterministicRNG

# Standard European wheel color layout (0 is green, not a bet color).
_RED_NUMBERS = frozenset({1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34, 36})

VALID_BET_TYPES = frozenset({
    "straight", "red", "black", "odd", "even", "low", "high",
    "dozen1", "dozen2", "dozen3", "col1", "col2", "col3",
})

# Total payout multiplier per unit staked, INCLUDING the returned stake (a
# winning 10-unit straight bet returns 360, not 350) -- avoids off-by-one
# confusion between "35:1 odds" and "pays 36x".
_PAYOUT_MULTIPLIER = {
    "straight": 36, "red": 2, "black": 2, "odd": 2, "even": 2, "low": 2, "high": 2,
    "dozen1": 3, "dozen2": 3, "dozen3": 3, "col1": 3, "col2": 3, "col3": 3,
}


def spin_number(seed: bytes) -> int:
    """Deterministic, unbiased spin in [0, 36] from the revealed seed."""
    return DeterministicRNG(seed).randbelow(37)


def color_of(number: int) -> str:
    if number == 0:
        return "green"
    return "red" if number in _RED_NUMBERS else "black"


def is_valid_bet(bet_type: str, bet_number: int) -> bool:
    if bet_type not in VALID_BET_TYPES:
        return False
    if bet_type == "straight":
        return 0 <= bet_number <= 36
    return True


def bet_wins(bet_type: str, bet_number: int, spin: int) -> bool:
    """Does this bet win against the given spin? Assumes is_valid_bet already
    passed -- callers must validate before settling real money on this."""
    if bet_type == "straight":
        return bet_number == spin
    if spin == 0:
        return False  # green: every even-money/dozen/column bet loses on zero
    if bet_type == "red":
        return color_of(spin) == "red"
    if bet_type == "black":
        return color_of(spin) == "black"
    if bet_type == "odd":
        return spin % 2 == 1
    if bet_type == "even":
        return spin % 2 == 0
    if bet_type == "low":
        return 1 <= spin <= 18
    if bet_type == "high":
        return 19 <= spin <= 36
    if bet_type == "dozen1":
        return 1 <= spin <= 12
    if bet_type == "dozen2":
        return 13 <= spin <= 24
    if bet_type == "dozen3":
        return 25 <= spin <= 36
    if bet_type == "col1":
        return spin % 3 == 1
    if bet_type == "col2":
        return spin % 3 == 2
    if bet_type == "col3":
        return spin % 3 == 0
    return False


def payout_for(bet_type: str, bet_number: int, amount: int, spin: int) -> int:
    """Total uCNPY paid to this bettor (0 if they lost, stake included if won)."""
    if not bet_wins(bet_type, bet_number, spin):
        return 0
    return amount * _PAYOUT_MULTIPLIER[bet_type]
