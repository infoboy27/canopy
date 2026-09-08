"""Unit tests for contract.game.domino -- pure logic, no chain/network."""

import unittest

from contract.game import domino as dom
from contract.game.domino import Move, IllegalMove


SEED = b"s" * 32


class TestDeal(unittest.TestCase):
    def test_deterministic_for_same_seed(self):
        h1, b1 = dom.deal(SEED)
        h2, b2 = dom.deal(SEED)
        self.assertEqual(h1, h2)
        self.assertEqual(b1, b2)

    def test_different_seeds_differ(self):
        h1, _ = dom.deal(b"a" * 32)
        h2, _ = dom.deal(b"b" * 32)
        self.assertNotEqual(h1, h2)

    def test_all_28_tiles_dealt_exactly_once(self):
        hands, boneyard = dom.deal(SEED)
        all_tiles = [t for h in hands for t in h] + boneyard
        self.assertEqual(len(all_tiles), 28)
        self.assertEqual(sorted(all_tiles), sorted(dom.FULL_SET))

    def test_hand_sizes(self):
        hands, boneyard = dom.deal(SEED)
        self.assertEqual(len(hands), 2)
        self.assertEqual(len(hands[0]), 7)
        self.assertEqual(len(hands[1]), 7)
        self.assertEqual(len(boneyard), 14)


class TestPipCount(unittest.TestCase):
    def test_empty_hand(self):
        self.assertEqual(dom.pip_count([]), 0)

    def test_sums_both_sides(self):
        self.assertEqual(dom.pip_count([(1, 2), (3, 3), (0, 6)]), 1 + 2 + 3 + 3 + 0 + 6)


def find_seed_where_p0_can_play_first(start=0):
    """p0's opening hand is always playable (any tile is legal with an empty
    board) -- this just finds a seed for readable, reproducible test fixtures."""
    for i in range(1000):
        seed = f"seed-{start + i}".encode().ljust(32, b"\x00")
        hands, _ = dom.deal(seed)
        if hands[0]:
            return seed, hands
    raise AssertionError("no seed found")


class TestReplayHappyPath(unittest.TestCase):
    def test_emptying_hand_wins_immediately(self):
        seed, hands = find_seed_where_p0_can_play_first()
        opening = hands[0][0]
        moves = [Move(action="play", tile=opening)]
        # Keep playing p0's remaining tiles against whichever end matches,
        # alternating with p1 passing/drawing as needed, until p0's hand is
        # empty -- simplest deterministic way to build a valid winning log
        # is to replay step by step against the real engine state.
        result = _play_out_fastest_win(seed)
        self.assertEqual(result.reason, "emptied_hand")
        self.assertIn(result.winners[0], (0, 1))
        self.assertEqual(len(result.final_hands[result.winners[0]]), 0)


def _legal_move_for(hand, ends):
    """Test helper: find any single legal play, or a draw/pass otherwise --
    NOT part of the module under test, just a way to generate valid logs."""
    if ends is None:
        return Move(action="play", tile=hand[0])
    left, right = ends
    for t in hand:
        a, b = t
        if a == left or b == left:
            return Move(action="play", tile=t, end="left")
        if a == right or b == right:
            return Move(action="play", tile=t, end="right")
    return None


def _play_out_fastest_win(seed):
    """Drives a full legal game using the simplest available strategy (first
    legal tile found) purely to produce a valid move log to feed back into
    dom.replay -- exercises the same rules the module enforces."""
    hands, boneyard = dom.deal(seed)
    ends = None
    turn = 0
    boneyard_idx = 0
    consecutive_passes = 0
    log = []

    while True:
        hand = hands[turn]
        mv = _legal_move_for(hand, ends)
        if mv is not None:
            log.append(mv)
            tile = tuple(sorted(mv.tile))
            hand.remove(tile)
            if ends is None:
                ends = tile
            else:
                left, right = ends
                a, b = tile
                if mv.end == "left":
                    new_value = b if a == left else a
                    ends = (new_value, right)
                else:
                    new_value = b if a == right else a
                    ends = (left, new_value)
            consecutive_passes = 0
            if not hand:
                return dom.replay(seed, log)
            turn = (turn + 1) % dom.NUM_PLAYERS
        elif boneyard_idx < len(boneyard):
            log.append(Move(action="draw"))
            hand.append(boneyard[boneyard_idx])
            boneyard_idx += 1
            consecutive_passes = 0
        else:
            log.append(Move(action="pass"))
            consecutive_passes += 1
            if consecutive_passes >= dom.NUM_PLAYERS:
                return dom.replay(seed, log)
            turn = (turn + 1) % dom.NUM_PLAYERS


class TestReplayFullGames(unittest.TestCase):
    def test_many_seeds_produce_a_valid_replayable_result(self):
        for i in range(50):
            seed = f"full-game-{i}".encode().ljust(32, b"\x00")
            result = _play_out_fastest_win(seed)
            self.assertIn(result.reason, ("emptied_hand", "blocked"))
            if result.reason == "emptied_hand":
                self.assertEqual(len(result.final_hands[result.winners[0]]), 0)


class TestIllegalMoves(unittest.TestCase):
    def test_playing_a_tile_not_in_hand_rejected(self):
        seed, hands = find_seed_where_p0_can_play_first()
        not_owned = next(t for t in dom.FULL_SET if t not in hands[0])
        with self.assertRaises(IllegalMove):
            dom.replay(seed, [Move(action="play", tile=not_owned)])

    def test_second_move_without_end_rejected(self):
        seed, hands = find_seed_where_p0_can_play_first()
        opening = hands[0][0]
        # p1's second move needs `end` set; a tile alone isn't enough once
        # the board is non-empty.
        p1_hand = hands[1]
        any_tile = p1_hand[0]
        with self.assertRaises(IllegalMove):
            dom.replay(seed, [Move(action="play", tile=opening), Move(action="play", tile=any_tile)])

    def test_playing_against_a_non_matching_end_rejected(self):
        seed, hands = find_seed_where_p0_can_play_first()
        opening = hands[0][0]
        left, right = opening
        # find a p1 tile that matches NEITHER end
        bad = next((t for t in hands[1] if left not in t and right not in t), None)
        if bad is None:
            self.skipTest("no counter-example tile in this seed's p1 hand")
        with self.assertRaises(IllegalMove):
            dom.replay(seed, [Move(action="play", tile=opening), Move(action="play", tile=bad, end="left")])

    def test_draw_with_legal_play_available_rejected(self):
        seed, hands = find_seed_where_p0_can_play_first()
        with self.assertRaises(IllegalMove):
            dom.replay(seed, [Move(action="draw")])

    def test_pass_with_legal_play_available_rejected(self):
        seed, hands = find_seed_where_p0_can_play_first()
        with self.assertRaises(IllegalMove):
            dom.replay(seed, [Move(action="pass")])

    def test_incomplete_log_rejected(self):
        seed, hands = find_seed_where_p0_can_play_first()
        opening = hands[0][0]
        with self.assertRaises(IllegalMove):
            dom.replay(seed, [Move(action="play", tile=opening)])

    def test_unknown_action_rejected(self):
        seed, _ = find_seed_where_p0_can_play_first()
        with self.assertRaises(IllegalMove):
            dom.replay(seed, [Move(action="fold")])


class TestBlockedGameScoring(unittest.TestCase):
    def test_lowest_pip_hand_wins_a_block(self):
        # Force an immediate block: both players pass on turn 1 requires no
        # legal play and an already-empty boneyard, which can't happen before
        # any tile is placed -- so directly exercise the scoring helper with
        # synthetic hands instead of hunting for a real blocked seed.
        hands = [[(6, 6), (5, 6)], [(0, 0), (0, 1)]]
        self.assertEqual(dom._lowest_pip_winners(hands), [1])

    def test_tie_returns_both_winners(self):
        hands = [[(1, 2)], [(0, 3)]]  # both total 3 pips
        self.assertEqual(dom._lowest_pip_winners(hands), [0, 1])


if __name__ == "__main__":
    unittest.main()
