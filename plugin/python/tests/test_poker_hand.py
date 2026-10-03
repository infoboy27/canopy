"""Unit tests for contract.game.poker's deck/hand-evaluation -- pure logic,
no chain/network, no betting yet."""

import unittest

from contract.game import poker as pk


class TestDeal(unittest.TestCase):
    def test_deterministic_for_same_seed(self):
        h1, b1 = pk.deal(b"s" * 32)
        h2, b2 = pk.deal(b"s" * 32)
        self.assertEqual(h1, h2)
        self.assertEqual(b1, b2)

    def test_no_duplicate_cards_across_hole_and_board(self):
        hole, board = pk.deal(b"x" * 32)
        all_cards = hole[0] + hole[1] + board
        self.assertEqual(len(all_cards), 9)
        self.assertEqual(len(set(all_cards)), 9)

    def test_hole_card_counts(self):
        hole, board = pk.deal(b"y" * 32)
        self.assertEqual(len(hole), 2)
        self.assertEqual(len(hole[0]), 2)
        self.assertEqual(len(hole[1]), 2)
        self.assertEqual(len(board), 5)


def C(spec):
    """'Ah' -> ('A','h')."""
    return (spec[0], spec[1])


def hand(*specs):
    return [C(s) for s in specs]


class TestHandRankCategories(unittest.TestCase):
    def test_high_card(self):
        r = pk.hand_rank(hand("Ah", "Ks", "9d", "5c", "2h"))
        self.assertEqual(pk.hand_category_name(r), "high_card")

    def test_pair(self):
        r = pk.hand_rank(hand("Ah", "As", "9d", "5c", "2h"))
        self.assertEqual(pk.hand_category_name(r), "pair")

    def test_two_pair(self):
        r = pk.hand_rank(hand("Ah", "As", "9d", "9c", "2h"))
        self.assertEqual(pk.hand_category_name(r), "two_pair")

    def test_trips(self):
        r = pk.hand_rank(hand("Ah", "As", "Ad", "5c", "2h"))
        self.assertEqual(pk.hand_category_name(r), "trips")

    def test_straight(self):
        r = pk.hand_rank(hand("5h", "6s", "7d", "8c", "9h"))
        self.assertEqual(pk.hand_category_name(r), "straight")

    def test_wheel_straight_ace_plays_low(self):
        r = pk.hand_rank(hand("Ah", "2s", "3d", "4c", "5h"))
        self.assertEqual(pk.hand_category_name(r), "straight")
        self.assertEqual(r, (4, 5))  # straight to the 5, not to the ace

    def test_flush(self):
        r = pk.hand_rank(hand("Ah", "Kh", "9h", "5h", "2h"))
        self.assertEqual(pk.hand_category_name(r), "flush")

    def test_full_house(self):
        r = pk.hand_rank(hand("Ah", "As", "Ad", "5c", "5h"))
        self.assertEqual(pk.hand_category_name(r), "full_house")

    def test_quads(self):
        r = pk.hand_rank(hand("Ah", "As", "Ad", "Ac", "5h"))
        self.assertEqual(pk.hand_category_name(r), "quads")

    def test_straight_flush(self):
        r = pk.hand_rank(hand("5h", "6h", "7h", "8h", "9h"))
        self.assertEqual(pk.hand_category_name(r), "straight_flush")

    def test_category_ordering_is_correct(self):
        categories = [
            hand("Ah", "Kd", "9s", "5c", "2h"),   # high card
            hand("Ah", "As", "9d", "5c", "2h"),   # pair
            hand("Ah", "As", "9d", "9c", "2h"),   # two pair
            hand("Ah", "As", "Ad", "5c", "2h"),   # trips
            hand("5h", "6s", "7d", "8c", "9h"),   # straight
            hand("Ah", "Kh", "9h", "5h", "2h"),   # flush
            hand("Ah", "As", "Ad", "5c", "5h"),   # full house
            hand("Ah", "As", "Ad", "Ac", "5h"),   # quads
            hand("5h", "6h", "7h", "8h", "9h"),   # straight flush
        ]
        ranks = [pk.hand_rank(c) for c in categories]
        self.assertEqual(ranks, sorted(ranks))  # strictly increasing by category


class TestHandRankTiebreaks(unittest.TestCase):
    def test_higher_pair_wins(self):
        a = pk.hand_rank(hand("Ah", "As", "9d", "5c", "2h"))
        b = pk.hand_rank(hand("Kh", "Ks", "9d", "5c", "2h"))
        self.assertGreater(a, b)

    def test_same_pair_higher_kicker_wins(self):
        a = pk.hand_rank(hand("Ah", "As", "9d", "5c", "3h"))
        b = pk.hand_rank(hand("Ah", "Ad", "9d", "5c", "2h"))
        self.assertGreater(a, b)

    def test_two_pair_top_pair_breaks_tie(self):
        a = pk.hand_rank(hand("Ah", "As", "Kd", "Kc", "2h"))
        b = pk.hand_rank(hand("Qh", "Qs", "Kd", "Kc", "2h"))
        self.assertGreater(a, b)

    def test_full_house_trips_rank_breaks_tie(self):
        a = pk.hand_rank(hand("Ah", "As", "Ad", "2c", "2h"))
        b = pk.hand_rank(hand("Kh", "Ks", "Kd", "Ac", "Ah"))
        self.assertGreater(a, b)

    def test_flush_high_card_breaks_tie(self):
        a = pk.hand_rank(hand("Ah", "Jh", "9h", "5h", "2h"))
        b = pk.hand_rank(hand("Kh", "Jh", "9h", "5h", "2h"))
        self.assertGreater(a, b)


class TestBestHandRankFromSeven(unittest.TestCase):
    def test_picks_the_best_five_of_seven(self):
        # hole = pocket aces, board makes a straight flush that doesn't use
        # the hole cards at all -- best hand should be the straight flush,
        # not just a pair of aces.
        seven = hand("Ah", "Ad", "2s", "3s", "4s", "5s", "6s")
        r = pk.best_hand_rank(seven)
        self.assertEqual(pk.hand_category_name(r), "straight_flush")

    def test_two_players_same_board_hole_cards_decide(self):
        board = hand("Kh", "Kd", "9s", "5c", "2h")
        p1 = pk.best_hand_rank(hand("Ah", "Ad") + board)   # two pair, kings and aces
        p2 = pk.best_hand_rank(hand("Qh", "Qd") + board)   # two pair, kings and queens
        self.assertGreater(p1, p2)

    def test_identical_hands_tie(self):
        board = hand("Kh", "Kd", "9s", "5c", "2h")
        p1 = pk.best_hand_rank(hand("Ah", "Jd") + board)
        p2 = pk.best_hand_rank(hand("Ac", "Js") + board)
        self.assertEqual(p1, p2)


if __name__ == "__main__":
    unittest.main()
