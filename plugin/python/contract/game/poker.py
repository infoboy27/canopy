"""Poker: heads-up (2-player) No-Limit Texas Hold'em, fixed blinds, no side
pots (deferred -- side pots only matter with 3+ players who go all-in at
different stack sizes). Same trustless philosophy as Domino, extended
further: not just move legality but a full best-of-7 hand evaluation at
showdown, and money amounts (bets/raises/calls) instead of simple tile
placement.

Same architecture as Domino: settle carries the revealed seed AND the full
action log; the plugin replays both the deal (from the seed) and every
betting action (validated legal at the time it was made) to derive the
winner itself. GameEngine drives the rules action-by-action (for a live
gameserver refereeing the hand street by street); replay() is a thin
wrapper over the same engine for settling a complete log in one shot.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from itertools import combinations
from typing import List, Optional, Tuple

from .rng import DeterministicRNG

NUM_PLAYERS = 2  # v1: heads-up only

RANKS = "23456789TJQKA"
SUITS = "shdc"
RANK_VALUE = {r: i + 2 for i, r in enumerate(RANKS)}

Card = Tuple[str, str]  # (rank_char, suit_char)
FULL_DECK: List[Card] = [(r, s) for r in RANKS for s in SUITS]  # 52 cards

HAND_CATEGORY_NAMES = [
    "high_card", "pair", "two_pair", "trips", "straight",
    "flush", "full_house", "quads", "straight_flush",
]


def deal(seed: bytes) -> Tuple[List[List[Card]], List[Card]]:
    """Deterministic shuffle + deal from the seed. hole[i] is player i's 2
    hole cards; the next 5 cards are the full board (flop+turn+river),
    revealed progressively as betting streets close."""
    order = DeterministicRNG(seed).shuffle(FULL_DECK)
    hole = [list(order[i * 2:(i + 1) * 2]) for i in range(NUM_PLAYERS)]
    board = list(order[NUM_PLAYERS * 2:NUM_PLAYERS * 2 + 5])
    return hole, board


def hand_rank(cards5: List[Card]):
    """Comparable score for exactly 5 cards -- higher tuple sorts higher."""
    ranks = sorted((RANK_VALUE[r] for r, _ in cards5), reverse=True)
    suits = [s for _, s in cards5]
    is_flush = len(set(suits)) == 1

    unique_ranks = sorted(set(ranks), reverse=True)
    is_straight = False
    straight_high = None
    if len(unique_ranks) == 5:
        if unique_ranks[0] - unique_ranks[4] == 4:
            is_straight = True
            straight_high = unique_ranks[0]
        elif unique_ranks == [14, 5, 4, 3, 2]:  # wheel: A-2-3-4-5, ace plays low
            is_straight = True
            straight_high = 5

    counts = Counter(ranks)
    by_count = sorted(counts.items(), key=lambda item: (-item[1], -item[0]))
    pattern = tuple(count for _, count in by_count)

    if is_straight and is_flush:
        return (8, straight_high)
    if pattern == (4, 1):
        return (7, by_count[0][0], by_count[1][0])
    if pattern == (3, 2):
        return (6, by_count[0][0], by_count[1][0])
    if is_flush:
        return (5, *ranks)
    if is_straight:
        return (4, straight_high)
    if pattern == (3, 1, 1):
        kickers = sorted((by_count[1][0], by_count[2][0]), reverse=True)
        return (3, by_count[0][0], *kickers)
    if pattern == (2, 2, 1):
        pairs = sorted((by_count[0][0], by_count[1][0]), reverse=True)
        return (2, *pairs, by_count[2][0])
    if pattern == (2, 1, 1, 1):
        kickers = sorted((by_count[1][0], by_count[2][0], by_count[3][0]), reverse=True)
        return (1, by_count[0][0], *kickers)
    return (0, *ranks)


def best_hand_rank(cards7: List[Card]):
    """Best 5-card hand rank achievable from up to 7 cards (2 hole + up to 5
    board). Brute force over all C(n,5) combinations -- n<=7 so this is at
    most 21 evaluations, cheap and simple to get right."""
    return max(hand_rank(list(combo)) for combo in combinations(cards7, 5))


def hand_category_name(rank) -> str:
    return HAND_CATEGORY_NAMES[rank[0]]


# ── Betting engine ──────────────────────────────────────────────────────

STREETS = ("preflop", "flop", "turn", "river")


@dataclass
class PokerAction:
    """Only two verbs -- 'check_call' always pays exactly min(to_call, stack)
    with no amount to get wrong; 'bet_raise' takes the TOTAL this player will
    have contributed to the current street after the action (not a delta),
    same convention as Domino's end-of-street contribution tracking."""
    action: str  # 'fold' | 'check_call' | 'bet_raise'
    amount: int = 0  # only meaningful for 'bet_raise'


class IllegalMove(Exception):
    """An action was not legal at the time it was made. Callers must treat
    this as a rejected claim/action, never patch over it."""


@dataclass
class HandResult:
    winners: List[int]
    reason: str  # 'fold' | 'showdown'
    pot: int                       # final contested pot, post uncalled-bet refund, PRE-rake
    stacks_remaining: List[int]    # chips never put in the pot this hand, always returned untaxed
    board: List[Card]              # community cards actually revealed
    hole: List[List[Card]]


class PokerEngine:
    """Heads-up No-Limit Hold'em, fixed blinds, no side pots. Player 0 is
    the dealer/small blind (acts first preflop, last postflop); player 1 is
    the big blind (acts last preflop, first postflop) -- the standard
    heads-up convention, which is the reverse of 3+-handed play."""

    def __init__(self, seed: bytes, small_blind: int, big_blind: int, stacks: Tuple[int, int]):
        if small_blind <= 0 or big_blind <= small_blind:
            raise ValueError("big_blind must be greater than a positive small_blind")
        self.hole, self.board_full = deal(seed)
        self.small_blind = small_blind
        self.big_blind = big_blind
        self.stacks = list(stacks)
        self.total_contributed = [0, 0]
        self.street = "preflop"
        self.board_revealed = 0
        self.pot = 0
        self.street_contributed = [0, 0]
        self.acted_this_street = [False, False]
        self.folded = [False, False]
        self.all_in = [False, False]
        self.last_raise_size = big_blind
        self.result: Optional[HandResult] = None

        self._post_blind(0, small_blind)
        self._post_blind(1, big_blind)
        self.turn = 0  # dealer/SB acts first preflop
        if self.all_in[0]:
            # a stack too short to fully cover its own blind can't act at all
            self.turn = 1 if not self.all_in[1] else 0
            if self.all_in[1]:
                self._runout_to_showdown()

    def _post_blind(self, player: int, amount: int) -> None:
        pay = min(amount, self.stacks[player])
        self.stacks[player] -= pay
        self.street_contributed[player] = pay
        self.total_contributed[player] += pay
        self.pot += pay
        if self.stacks[player] == 0:
            self.all_in[player] = True

    @property
    def finished(self) -> bool:
        return self.result is not None

    def apply_action(self, action: PokerAction) -> Optional[HandResult]:
        """Always acts for self.turn -- there is nothing ambiguous about
        whose turn it is (same reasoning as Domino's GameEngine), so callers
        never pass a player index. A gameserver mapping an incoming request's
        wallet address to a seat validates that seat == self.turn BEFORE
        calling this, at the manager level, not here."""
        if self.finished:
            raise IllegalMove("hand already finished")
        player = self.turn
        if self.folded[player] or self.all_in[player]:
            raise IllegalMove(f"player {player} cannot act (folded or all-in)")

        to_call = max(self.street_contributed) - self.street_contributed[player]

        if action.action == "fold":
            self.folded[player] = True
            return self._finish(winners=[1 - player], reason="fold")

        if action.action == "check_call":
            pay = min(to_call, self.stacks[player])
            self.stacks[player] -= pay
            self.street_contributed[player] += pay
            self.total_contributed[player] += pay
            self.pot += pay
            self.acted_this_street[player] = True
            if self.stacks[player] == 0:
                self.all_in[player] = True

        elif action.action == "bet_raise":
            current_bet = max(self.street_contributed)
            total_committed = action.amount
            delta = total_committed - self.street_contributed[player]
            if delta <= 0:
                raise IllegalMove("bet/raise must increase this player's contribution")
            if delta > self.stacks[player]:
                raise IllegalMove("insufficient stack for this action")
            is_all_in = delta == self.stacks[player]
            min_legal_total = current_bet + max(self.last_raise_size, self.big_blind) if current_bet > 0 else self.big_blind
            if total_committed < min_legal_total and not is_all_in:
                raise IllegalMove(f"bet/raise below the legal minimum ({min_legal_total})")
            raise_size = total_committed - current_bet
            self.stacks[player] -= delta
            self.street_contributed[player] = total_committed
            self.total_contributed[player] += delta
            self.pot += delta
            self.acted_this_street[player] = True
            if is_all_in:
                self.all_in[player] = True
            self.last_raise_size = max(raise_size, self.big_blind)
            self.acted_this_street[1 - player] = False  # opponent must respond

        else:
            raise IllegalMove(f"unknown action {action.action!r}")

        return self._advance()

    def _advance(self) -> Optional[HandResult]:
        active = [i for i in range(2) if not self.folded[i]]
        non_all_in = [i for i in active if not self.all_in[i]]
        if not non_all_in:
            # everyone left is all-in -- no further action is possible at all
            return self._runout_to_showdown()
        if len(non_all_in) < len(active):
            # one side is all-in and can never act again; done once the
            # other side has responded (contribution can't be equalized any
            # further since the all-in side has nothing left to put in)
            if all(self.acted_this_street[i] for i in non_all_in):
                return self._runout_to_showdown()
            self.turn = non_all_in[0]
            return None
        contributed_equal = self.street_contributed[0] == self.street_contributed[1]
        if contributed_equal and all(self.acted_this_street[i] for i in non_all_in):
            return self._close_street()
        self.turn = 1 - self.turn
        return None

    def _close_street(self) -> Optional[HandResult]:
        if self.street == "river":
            return self._showdown()
        self.street = {"preflop": "flop", "flop": "turn", "turn": "river"}[self.street]
        self.board_revealed = {"flop": 3, "turn": 4, "river": 5}[self.street]
        self.street_contributed = [0, 0]
        self.acted_this_street = [False, False]
        self.last_raise_size = self.big_blind
        self.turn = 1  # heads-up postflop: BB acts first, dealer/SB acts last
        return None

    def _runout_to_showdown(self) -> HandResult:
        self.board_revealed = 5
        return self._showdown()

    def _showdown(self) -> HandResult:
        active = [i for i in range(2) if not self.folded[i]]
        scores = {i: best_hand_rank(self.hole[i] + self.board_full) for i in active}
        best = max(scores.values())
        winners = [i for i in active if scores[i] == best]
        return self._finish(winners=winners, reason="showdown")

    def _finish(self, winners: List[int], reason: str) -> HandResult:
        # Uncalled excess: only refund when the SHORT side is all-in (i.e.
        # physically could not put in more) -- a plain SB/BB size difference
        # is normal blind structure, not an uncalled bet, and must NOT be
        # refunded (e.g. an immediate fold after blinds awards the whole
        # SB+BB pot to the other player, it doesn't partially refund the BB).
        if self.total_contributed[0] != self.total_contributed[1]:
            hi = 0 if self.total_contributed[0] > self.total_contributed[1] else 1
            lo = 1 - hi
            if self.all_in[lo]:
                refund = self.total_contributed[hi] - self.total_contributed[lo]
                self.stacks[hi] += refund
                self.total_contributed[hi] -= refund
                self.pot -= refund
        self.result = HandResult(
            winners=winners, reason=reason, pot=self.pot,
            stacks_remaining=list(self.stacks),
            board=list(self.board_full[:self.board_revealed]), hole=self.hole,
        )
        return self.result


def replay(seed: bytes, small_blind: int, big_blind: int, stacks: Tuple[int, int],
           actions: List[PokerAction]) -> HandResult:
    """actions is a flat list of PokerAction in submission order -- no player
    index needed, since whose turn it is is fully determined by the engine
    itself. Settles on-chain: the plugin replays a claimed log exactly like
    this to independently derive the winner and pot before paying anyone."""
    engine = PokerEngine(seed, small_blind, big_blind, stacks)
    for action in actions:
        result = engine.apply_action(action)
        if result is not None:
            return result
    raise IllegalMove("action log ended without a winner -- incomplete hand claim")
