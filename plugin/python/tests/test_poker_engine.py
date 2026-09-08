"""Unit tests for contract.game.poker's betting engine (PokerEngine/replay)
-- pure logic, no chain/network."""

import unittest

from contract.game import poker as pk
from contract.game.poker import PokerAction as A
from contract.game.poker import IllegalMove


SEED = b"s" * 32
SB, BB = 10, 20
STACKS = (1000, 1000)


def new_engine(seed=SEED, small_blind=SB, big_blind=BB, stacks=STACKS):
    return pk.PokerEngine(seed, small_blind, big_blind, stacks)


class TestBlinds(unittest.TestCase):
    def test_blinds_posted_correctly(self):
        e = new_engine()
        self.assertEqual(e.street_contributed, [SB, BB])
        self.assertEqual(e.stacks, [1000 - SB, 1000 - BB])
        self.assertEqual(e.pot, SB + BB)
        self.assertEqual(e.turn, 0)  # dealer/SB acts first preflop

    def test_rejects_big_blind_not_greater_than_small_blind(self):
        with self.assertRaises(ValueError):
            new_engine(small_blind=20, big_blind=20)


class TestBasicHandToShowdown(unittest.TestCase):
    def test_check_down_all_streets_reaches_showdown(self):
        e = new_engine()
        # preflop: SB calls (matches BB), BB checks
        self.assertIsNone(e.apply_action(A("check_call")))
        self.assertIsNone(e.apply_action(A("check_call")))
        self.assertEqual(e.street, "flop")
        # flop: BB acts first heads-up postflop
        self.assertIsNone(e.apply_action(A("check_call")))
        self.assertIsNone(e.apply_action(A("check_call")))
        self.assertEqual(e.street, "turn")
        self.assertIsNone(e.apply_action(A("check_call")))
        self.assertIsNone(e.apply_action(A("check_call")))
        self.assertEqual(e.street, "river")
        result = e.apply_action(A("check_call"))
        self.assertIsNone(result)
        result = e.apply_action(A("check_call"))
        self.assertIsNotNone(result)
        self.assertEqual(result.reason, "showdown")
        self.assertEqual(len(result.board), 5)

    def test_pot_matches_total_contributed_when_no_fold(self):
        e = new_engine()
        for _ in range(2):
            e.apply_action(A("check_call"))
        for _ in range(3):
            for _ in range(2):
                e.apply_action(A("check_call"))
        # all four streets checked down; pot should equal 2x big blind (both matched BB, no further betting)
        self.assertEqual(e.pot, BB * 2)


class TestFold(unittest.TestCase):
    def test_fold_ends_hand_immediately_awarding_the_pot(self):
        e = new_engine()
        result = e.apply_action(A("fold"))
        self.assertEqual(result.winners, [1])
        self.assertEqual(result.reason, "fold")
        self.assertEqual(result.pot, SB + BB)
        # folder's remaining stack is untouched beyond the blind already posted
        self.assertEqual(result.stacks_remaining[0], 1000 - SB)
        self.assertEqual(result.stacks_remaining[1], 1000 - BB)

    def test_cannot_act_after_hand_is_finished(self):
        e = new_engine()
        e.apply_action(A("fold"))
        with self.assertRaises(IllegalMove):
            e.apply_action(A("check_call"))


class TestTurnAndLegalityValidation(unittest.TestCase):
    def test_check_when_facing_a_bet_is_rejected_via_check_call_underpay_not_bug(self):
        # check_call always pays whatever is owed -- there's no separate
        # 'check' the engine could reject; verify facing a bet still costs
        # the caller the right amount instead of being a free pass.
        e = new_engine()
        e.apply_action(A("bet_raise", amount=BB * 3))  # SB raises to 3x the BB (legal: raise size >= BB)
        before = e.stacks[1]
        e.apply_action(A("check_call"))
        self.assertLess(e.stacks[1], before)

    def test_raise_below_minimum_rejected(self):
        e = new_engine()
        with self.assertRaises(IllegalMove):
            e.apply_action(A("bet_raise", amount=BB + 1))  # raise of only 1 over BB, min raise is BB

    def test_bet_that_does_not_increase_contribution_rejected(self):
        e = new_engine()
        with self.assertRaises(IllegalMove):
            e.apply_action(A("bet_raise", amount=SB))  # already contributed SB; not an increase

    def test_bet_exceeding_stack_rejected(self):
        e = new_engine()
        with self.assertRaises(IllegalMove):
            e.apply_action(A("bet_raise", amount=10_000))

    def test_folded_player_cannot_act(self):
        e = new_engine()
        e.apply_action(A("bet_raise", amount=BB * 3))
        e.apply_action(A("fold"))
        with self.assertRaises(IllegalMove):
            e.apply_action(A("check_call"))


class TestRaiseSequencing(unittest.TestCase):
    def test_raise_reopens_action_for_the_opponent(self):
        e = new_engine()
        e.apply_action(A("check_call"))  # SB completes
        e.apply_action(A("bet_raise", amount=BB * 3))  # BB raises
        # street should NOT have closed yet -- player 0 must respond
        self.assertEqual(e.street, "preflop")
        self.assertEqual(e.turn, 0)

    def test_min_raise_must_be_at_least_the_previous_raise_size(self):
        e = new_engine()
        e.apply_action(A("check_call"))
        e.apply_action(A("bet_raise", amount=BB * 3))  # raise of 2*BB over the BB
        with self.assertRaises(IllegalMove):
            # re-raise smaller than the previous raise increment (2*BB)
            e.apply_action(A("bet_raise", amount=BB * 3 + 1))


class TestAllIn(unittest.TestCase):
    def test_all_in_call_runs_out_the_board_immediately(self):
        e = new_engine(stacks=(100, 1000))
        e.apply_action(A("bet_raise", amount=100))  # SB shoves for their whole stack
        result = e.apply_action(A("check_call"))    # BB calls, matching exactly
        self.assertIsNotNone(result)
        self.assertEqual(result.reason, "showdown")
        self.assertEqual(len(result.board), 5)

    def test_short_all_in_call_refunds_the_uncalled_excess(self):
        e = new_engine(stacks=(1000, 30))  # BB can only ever put in 30 total
        e.apply_action(A("bet_raise", amount=200))  # SB bets 200
        result = e.apply_action(A("check_call"))    # BB can only call all-in for 30
        self.assertIsNotNone(result)
        # pot is capped at 2x the short stack's total contribution (30 * 2 = 60);
        # SB's uncalled excess (200 - 30 = 170) comes back untaxed.
        self.assertEqual(result.pot, 60)
        total_returned = sum(result.stacks_remaining) + result.pot
        self.assertEqual(total_returned, 1000 + 30)  # nothing created or destroyed


class TestReplay(unittest.TestCase):
    def test_replay_matches_direct_engine_usage(self):
        actions = [A("check_call")] * 8  # check/call down all four streets to showdown
        result = pk.replay(SEED, SB, BB, STACKS, actions)
        self.assertEqual(result.reason, "showdown")

    def test_incomplete_log_rejected(self):
        with self.assertRaises(IllegalMove):
            pk.replay(SEED, SB, BB, STACKS, [A("check_call")])

    def test_illegal_action_in_log_rejected(self):
        with self.assertRaises(IllegalMove):
            # a raise below the legal minimum as the very first action
            pk.replay(SEED, SB, BB, STACKS, [A("bet_raise", amount=BB + 1)])


class TestMoneyConservation(unittest.TestCase):
    def test_check_down_conserves_total_chips_across_many_seeds(self):
        for i in range(100):
            seed = f"fuzz-{i}".encode().ljust(32, b"\x00")
            e = pk.PokerEngine(seed, SB, BB, STACKS)
            result = None
            guard = 0
            while result is None:
                guard += 1
                self.assertLess(guard, 20, "hand should resolve within a handful of check/calls")
                result = e.apply_action(A("check_call"))
            self.assertEqual(sum(result.stacks_remaining) + result.pot, sum(STACKS))


if __name__ == "__main__":
    unittest.main()
