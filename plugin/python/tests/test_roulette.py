"""Unit tests for contract.game.roulette -- pure logic, no chain/network."""

import unittest

from contract.game import roulette as rl


class TestSpinNumber(unittest.TestCase):
    def test_deterministic_for_same_seed(self):
        seed = b"s" * 32
        self.assertEqual(rl.spin_number(seed), rl.spin_number(seed))

    def test_in_range(self):
        for i in range(200):
            seed = bytes([i]) * 32
            n = rl.spin_number(seed)
            self.assertGreaterEqual(n, 0)
            self.assertLessEqual(n, 36)

    def test_distribution_covers_the_wheel(self):
        # Not a statistical test -- just confirms randbelow(37) isn't
        # silently collapsing to a narrow range across many seeds.
        seen = {rl.spin_number(bytes([i, i, i]) * 11) for i in range(200)}
        self.assertGreater(len(seen), 25)


class TestColorOf(unittest.TestCase):
    def test_zero_is_green(self):
        self.assertEqual(rl.color_of(0), "green")

    def test_known_red_and_black(self):
        self.assertEqual(rl.color_of(1), "red")
        self.assertEqual(rl.color_of(2), "black")
        self.assertEqual(rl.color_of(36), "red")

    def test_every_number_has_exactly_one_color(self):
        for n in range(37):
            self.assertIn(rl.color_of(n), ("green", "red", "black"))

    def test_18_red_18_black_1_green(self):
        counts = {"red": 0, "black": 0, "green": 0}
        for n in range(37):
            counts[rl.color_of(n)] += 1
        self.assertEqual(counts, {"red": 18, "black": 18, "green": 1})


class TestIsValidBet(unittest.TestCase):
    def test_unknown_bet_type_rejected(self):
        self.assertFalse(rl.is_valid_bet("nonsense", 0))

    def test_straight_requires_valid_number(self):
        self.assertTrue(rl.is_valid_bet("straight", 0))
        self.assertTrue(rl.is_valid_bet("straight", 36))
        self.assertFalse(rl.is_valid_bet("straight", 37))

    def test_named_bets_ignore_number(self):
        self.assertTrue(rl.is_valid_bet("red", 999))


class TestBetWins(unittest.TestCase):
    def test_straight_exact_match_only(self):
        self.assertTrue(rl.bet_wins("straight", 17, 17))
        self.assertFalse(rl.bet_wins("straight", 17, 18))

    def test_zero_loses_every_outside_bet(self):
        for bet_type in ("red", "black", "odd", "even", "low", "high", "dozen1", "col1"):
            self.assertFalse(rl.bet_wins(bet_type, 0, spin=0))

    def test_red_black(self):
        self.assertTrue(rl.bet_wins("red", 0, spin=1))
        self.assertFalse(rl.bet_wins("black", 0, spin=1))

    def test_odd_even(self):
        self.assertTrue(rl.bet_wins("odd", 0, spin=17))
        self.assertTrue(rl.bet_wins("even", 0, spin=18))

    def test_low_high(self):
        self.assertTrue(rl.bet_wins("low", 0, spin=1))
        self.assertTrue(rl.bet_wins("low", 0, spin=18))
        self.assertFalse(rl.bet_wins("low", 0, spin=19))
        self.assertTrue(rl.bet_wins("high", 0, spin=36))

    def test_dozens_partition_1_to_36(self):
        self.assertTrue(rl.bet_wins("dozen1", 0, spin=12))
        self.assertFalse(rl.bet_wins("dozen1", 0, spin=13))
        self.assertTrue(rl.bet_wins("dozen2", 0, spin=13))
        self.assertTrue(rl.bet_wins("dozen3", 0, spin=36))

    def test_columns_are_mod_3(self):
        self.assertTrue(rl.bet_wins("col1", 0, spin=1))
        self.assertTrue(rl.bet_wins("col2", 0, spin=2))
        self.assertTrue(rl.bet_wins("col3", 0, spin=3))
        self.assertTrue(rl.bet_wins("col1", 0, spin=4))


class TestPayoutFor(unittest.TestCase):
    def test_loss_pays_zero(self):
        self.assertEqual(rl.payout_for("red", 0, 100, spin=2), 0)

    def test_straight_pays_36x_including_stake(self):
        self.assertEqual(rl.payout_for("straight", 17, 100, spin=17), 3600)

    def test_even_money_pays_2x(self):
        self.assertEqual(rl.payout_for("red", 0, 100, spin=1), 200)

    def test_dozen_pays_3x(self):
        self.assertEqual(rl.payout_for("dozen1", 0, 100, spin=5), 300)

    def test_house_edge_present_via_zero(self):
        # A red bettor loses their whole stake when the ball lands on 0 --
        # that's the house edge; confirm zero really does forfeit, not push.
        self.assertEqual(rl.payout_for("red", 0, 100, spin=0), 0)


if __name__ == "__main__":
    unittest.main()
