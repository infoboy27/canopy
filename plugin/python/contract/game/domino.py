"""Domino: block variant with boneyard draw, 2 players (heads-up), double-six
set. Same trustless philosophy as Bingo/Roulette, extended for a genuinely
interactive game: the outcome depends on which legal move each player CHOSE,
not just the seed. There is no pure function seed -> winner the way Bingo's
draw or Roulette's spin are -- so instead of settling on a bare seed reveal,
settle carries the full move log, and this module REPLAYS it against the
seed-derived deal, rejecting the claim outright if any move wasn't legal at
the time it was made. Anyone -- the plugin at settle, or an outside auditor --
replaying the same (seed, moves) pair gets the exact same answer.

GameEngine drives the rules move-by-move (for a live gameserver validating
each turn as it happens); replay() is a thin wrapper over the same engine
for settling a complete log in one shot, so both call sites are guaranteed
to agree on what's legal.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Tuple

from .rng import DeterministicRNG

HAND_SIZE = 7
NUM_PLAYERS = 2  # v1: heads-up only

Tile = Tuple[int, int]

# Canonical double-six set, 28 tiles, always stored (low, high).
FULL_SET: List[Tile] = [(a, b) for a in range(7) for b in range(a, 7)]


def deal(seed: bytes) -> Tuple[List[List[Tile]], List[Tile]]:
    """Deterministic shuffle + deal from the seed. hands[i] is player i's
    starting hand; the remainder is the boneyard, drawn from in shuffled
    order as players run out of legal plays."""
    order = DeterministicRNG(seed).shuffle(FULL_SET)
    hands = [list(order[i * HAND_SIZE:(i + 1) * HAND_SIZE]) for i in range(NUM_PLAYERS)]
    boneyard = list(order[NUM_PLAYERS * HAND_SIZE:])
    return hands, boneyard


def pip_count(tiles: List[Tile]) -> int:
    return sum(a + b for a, b in tiles)


def _has_legal_play(hand: List[Tile], ends: Optional[Tuple[int, int]]) -> bool:
    if ends is None:
        return len(hand) > 0
    left, right = ends
    return any(a == left or b == left or a == right or b == right for a, b in hand)


@dataclass
class Move:
    action: str                          # 'play' | 'draw' | 'pass'
    tile: Optional[Tile] = None           # required for 'play'
    end: Optional[str] = None             # 'left' | 'right' -- required for 'play' once the board is non-empty


class IllegalMove(Exception):
    """A move was not legal at the time it was made. Callers must treat this
    as a rejected claim/action, never patch over it."""


@dataclass
class GameResult:
    winners: List[int]                    # player indices; more than one on a tied block
    reason: str                           # 'emptied_hand' | 'blocked'
    final_hands: List[List[Tile]]


def _lowest_pip_winners(hands: List[List[Tile]]) -> List[int]:
    counts = [pip_count(h) for h in hands]
    lowest = min(counts)
    return [i for i, c in enumerate(counts) if c == lowest]


class GameEngine:
    """Stateful, incremental rules engine -- apply_move() validates and
    applies exactly one move, returning a GameResult once the game ends or
    None while it's still in progress. A live gameserver drives this turn by
    turn; replay() below drives the identical engine over a complete log in
    one pass for settling on-chain."""

    def __init__(self, seed: bytes):
        self.hands, self.boneyard = deal(seed)
        self.ends: Optional[Tuple[int, int]] = None
        self.turn = 0
        self.consecutive_passes = 0
        self.boneyard_idx = 0
        self.result: Optional[GameResult] = None

    @property
    def finished(self) -> bool:
        return self.result is not None

    def legal_play_exists(self, player: Optional[int] = None) -> bool:
        hand = self.hands[self.turn if player is None else player]
        return _has_legal_play(hand, self.ends)

    def apply_move(self, move: Move) -> Optional[GameResult]:
        if self.finished:
            raise IllegalMove("game already finished")
        turn = self.turn
        hand = self.hands[turn]

        if move.action == "draw":
            if _has_legal_play(hand, self.ends):
                raise IllegalMove(f"player {turn} drew with a legal play available")
            if self.boneyard_idx >= len(self.boneyard):
                raise IllegalMove(f"player {turn} drew from an empty boneyard")
            hand.append(self.boneyard[self.boneyard_idx])
            self.boneyard_idx += 1
            self.consecutive_passes = 0
            return None  # turn does not advance -- same player must act again

        if move.action == "pass":
            if _has_legal_play(hand, self.ends) or self.boneyard_idx < len(self.boneyard):
                raise IllegalMove(f"player {turn} passed with a legal play or boneyard tile available")
            self.consecutive_passes += 1
            if self.consecutive_passes >= NUM_PLAYERS:
                self.result = GameResult(winners=_lowest_pip_winners(self.hands), reason="blocked",
                                          final_hands=self.hands)
                return self.result
            self.turn = (turn + 1) % NUM_PLAYERS
            return None

        if move.action == "play":
            if move.tile is None:
                raise IllegalMove(f"player {turn} played with no tile specified")
            tile = tuple(sorted(move.tile))
            if tile not in hand:
                raise IllegalMove(f"player {turn} played a tile not in hand: {tile}")
            a, b = tile
            if self.ends is None:
                self.ends = (a, b)
            else:
                if move.end not in ("left", "right"):
                    raise IllegalMove(f"player {turn} played without a valid end")
                left, right = self.ends
                target = left if move.end == "left" else right
                if a == target:
                    new_value = b
                elif b == target:
                    new_value = a
                else:
                    raise IllegalMove(f"player {turn} played {tile} against end value {target}, no match")
                self.ends = (new_value, right) if move.end == "left" else (left, new_value)
            hand.remove(tile)
            self.consecutive_passes = 0
            if not hand:
                self.result = GameResult(winners=[turn], reason="emptied_hand", final_hands=self.hands)
                return self.result
            self.turn = (turn + 1) % NUM_PLAYERS
            return None

        raise IllegalMove(f"unknown action {move.action!r}")


def replay(seed: bytes, moves: List[Move]) -> GameResult:
    engine = GameEngine(seed)
    for move in moves:
        result = engine.apply_move(move)
        if result is not None:
            return result
    raise IllegalMove("move log ended without a winner -- incomplete game claim")
