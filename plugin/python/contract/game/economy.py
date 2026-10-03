"""Bingo Rush — game economy.

Single source of truth for currencies, rooms, entry costs, prize pools,
payouts, daily rewards, the shop and XP/leveling.

Design rules (important — this module is shared between the off-chain game
server AND the on-chain Canopy Python plugin):

* **Integer-only, deterministic math.** No floats anywhere in value flows.
  Percentages are expressed in basis points (bps, 1% = 100 bps) and applied
  with integer division so the game server and the chain always agree to the
  unit. Floats are non-deterministic across machines and would let an
  on-chain settlement disagree with the off-chain result.
* **Display units vs base units.** Balances here are whole coins / whole gems
  (what the UI shows). On-chain, amounts are stored as uint64 *base units*.
  Use `to_base_units` / `from_base_units` at the chain boundary only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Sequence

# ─────────────────────────────────────────────────────────────────────────────
# Currencies
# ─────────────────────────────────────────────────────────────────────────────


class Currency(str, Enum):
    COINS = "coins"  # soft currency: earned by playing, spent on entries/boosters
    GEMS = "gems"    # hard currency: bought with money, spent on premium items
    XP = "xp"        # experience: drives leveling, never spent


# On-chain denomination. Coins are divisible on the ledger (fees, precise
# payouts); gems are indivisible collectible units. Only convert at the
# chain boundary — never carry base units through game logic.
COIN_DECIMALS = 6
GEM_DECIMALS = 0


def _nonnegative_integer(value: int, name: str) -> None:
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")


def to_base_units(amount: int, currency: Currency) -> int:
    """Display units -> on-chain uint64 base units."""
    _nonnegative_integer(amount, "amount")
    decimals = COIN_DECIMALS if currency == Currency.COINS else GEM_DECIMALS
    result = amount * (10 ** decimals)
    if result > (1 << 64) - 1:
        raise ValueError("amount exceeds uint64 base units")
    return result


def from_base_units(base: int, currency: Currency) -> int:
    """On-chain base units -> whole display units (floor)."""
    _nonnegative_integer(base, "base")
    decimals = COIN_DECIMALS if currency == Currency.COINS else GEM_DECIMALS
    return int(base) // (10 ** decimals)


# ─────────────────────────────────────────────────────────────────────────────
# Rooms  (source: Lobby screen in the mockup)
# ─────────────────────────────────────────────────────────────────────────────


class Difficulty(str, Enum):
    EASY = "Easy"
    MEDIUM = "Medium"
    HARD = "Hard"
    ELITE = "Elite"


@dataclass(frozen=True)
class Room:
    """A game room / table.

    ``advertised_prize`` is the marketing figure shown in the lobby. The *real*
    prize pool is dynamic: it is the sum of every entry actually collected
    (see :func:`prize_pool`), minus the house ``rake_bps``.
    """

    id: str
    name: str
    emoji: str
    entry_fee: int              # coins to buy ONE card in this room
    capacity: int               # max simultaneous players
    difficulty: Difficulty
    advertised_prize: int       # lobby display number only
    rake_bps: int               # house cut, basis points (1000 = 10%)
    # payout split across finishing ranks, in bps summing to 10_000.
    # [10000] == winner-take-all; [7000,2000,1000] == top-3 split.
    payout_weights_bps: tuple[int, ...] = (10_000,)


ROOMS: dict[str, Room] = {
    "classic": Room("classic", "Classic Room", "🎱", 100, 20, Difficulty.EASY, 2_500, 1_000),
    "speed":   Room("speed", "Speed Bingo", "⚡", 250, 20, Difficulty.MEDIUM, 6_000, 1_000),
    "jackpot": Room("jackpot", "Jackpot Room", "💰", 500, 20, Difficulty.HARD, 25_000, 500,
                    payout_weights_bps=(7_000, 2_000, 1_000)),
    "vip":     Room("vip", "VIP Lounge", "👑", 1_000, 10, Difficulty.ELITE, 50_000, 500,
                    payout_weights_bps=(6_000, 2_500, 1_500)),
}


def get_room(room_id: str) -> Room:
    try:
        return ROOMS[room_id]
    except KeyError:
        raise ValueError(f"unknown room: {room_id!r}") from None


# ─────────────────────────────────────────────────────────────────────────────
# Cards & entry cost  (source: "Choose Cards" screen)
# ─────────────────────────────────────────────────────────────────────────────

MIN_CARDS = 1
MAX_CARDS = 4

# Cost multiplier for buying N cards, relative to one entry_fee. Buying more
# cards is progressively discounted vs paying N * entry_fee. These reproduce the
# mockup's classic-room costs [100, 180, 250, 320] at entry_fee = 100.
CARD_COST_MULTIPLIER_BPS: dict[int, int] = {
    1: 10_000,   # 1.0x
    2: 18_000,   # 1.8x
    3: 25_000,   # 2.5x
    4: 32_000,   # 3.2x
}

# Each extra card improves win odds (more numbers in play). +8% per card.
ODDS_BOOST_BPS_PER_CARD = 800


def entry_cost(room_id: str, num_cards: int) -> int:
    """Total coins to enter ``room_id`` with ``num_cards`` cards."""
    _validate_card_count(num_cards)
    room = get_room(room_id)
    return room.entry_fee * CARD_COST_MULTIPLIER_BPS[num_cards] // 10_000


def odds_boost_bps(num_cards: int) -> int:
    _validate_card_count(num_cards)
    return ODDS_BOOST_BPS_PER_CARD * num_cards


def card_selection(room_id: str) -> list[dict]:
    """The four purchase options rendered by the card-selection screen."""
    return [
        {
            "num_cards": n,
            "entry_cost": entry_cost(room_id, n),
            "odds_boost_bps": odds_boost_bps(n),
        }
        for n in range(MIN_CARDS, MAX_CARDS + 1)
    ]


def _validate_card_count(num_cards: int) -> None:
    if type(num_cards) is not int or num_cards not in CARD_COST_MULTIPLIER_BPS:
        raise ValueError(f"num_cards must be {MIN_CARDS}..{MAX_CARDS}, got {num_cards}")


# ─────────────────────────────────────────────────────────────────────────────
# Prize pool & payouts
# ─────────────────────────────────────────────────────────────────────────────


def prize_pool(entries: Sequence[int]) -> int:
    """Gross pool = every entry fee collected for the room."""
    for entry in entries:
        _nonnegative_integer(entry, "entry amount")
    return sum(entries)


def apply_rake(gross_pool: int, rake_bps: int) -> tuple[int, int]:
    """Split a gross pool into (net_to_players, house_take)."""
    _nonnegative_integer(gross_pool, "gross_pool")
    _nonnegative_integer(rake_bps, "rake_bps")
    if not 0 <= rake_bps <= 10_000:
        raise ValueError("rake_bps must be 0..10000")
    house = gross_pool * rake_bps // 10_000
    return gross_pool - house, house


def distribute(net_pool: int, weights_bps: Sequence[int]) -> list[int]:
    """Split ``net_pool`` across ranks by ``weights_bps`` (must sum to 10000).

    Integer division; any rounding remainder goes to the top rank so the sum of
    payouts always equals ``net_pool`` exactly (no coins created or lost).
    """
    _nonnegative_integer(net_pool, "net_pool")
    if not weights_bps or any(type(w) is not int or w <= 0 for w in weights_bps):
        raise ValueError("payout weights must be positive integers")
    if sum(weights_bps) != 10_000:
        raise ValueError("payout weights must sum to 10000 bps")
    payouts = [net_pool * w // 10_000 for w in weights_bps]
    payouts[0] += net_pool - sum(payouts)  # give remainder to the winner
    return payouts


@dataclass(frozen=True)
class RoomResult:
    gross_pool: int
    house_take: int
    payouts: list[int]          # coins paid to ranks 1..N
    winner_coins: int           # convenience: payouts[0]


def settle_room(room_id: str, entries: Sequence[int]) -> RoomResult:
    """Compute the full financial outcome of a finished room.

    ``entries`` is the list of entry fees collected (one per paying seat). The
    number of ranks paid is capped at the number of entries present.
    """
    room = get_room(room_id)
    gross = prize_pool(entries)
    net, house = apply_rake(gross, room.rake_bps)

    # Cannot pay more ranks than there are players.
    weights = list(room.payout_weights_bps[: max(1, len(entries))])
    if sum(weights) != 10_000:  # re-normalise if we truncated ranks
        total = sum(weights)
        weights = [w * 10_000 // total for w in weights]
        weights[0] += 10_000 - sum(weights)

    payouts = distribute(net, weights)
    return RoomResult(gross, house, payouts, payouts[0])


# ─────────────────────────────────────────────────────────────────────────────
# Match rewards  (source: Win / Lose screens)
# ─────────────────────────────────────────────────────────────────────────────

WIN_XP = 450                    # XP granted to the BINGO winner
CONSOLATION_COINS = 50          # coins for non-winners ("Consolation Prize!")
CONSOLATION_XP = 100            # XP for non-winners


@dataclass(frozen=True)
class Reward:
    coins: int = 0
    gems: int = 0
    xp: int = 0


def win_reward(room_id: str, entries: Sequence[int]) -> Reward:
    """Reward for taking rank 1 in a room."""
    result = settle_room(room_id, entries)
    return Reward(coins=result.winner_coins, xp=WIN_XP)


def rank_reward(room_id: str, entries: Sequence[int], rank: int) -> Reward:
    """Reward for finishing at ``rank`` (1-indexed). Ranks beyond the paid
    positions get the consolation reward."""
    if rank < 1:
        raise ValueError("rank is 1-indexed")
    result = settle_room(room_id, entries)
    if rank <= len(result.payouts) and result.payouts[rank - 1] > 0:
        xp = WIN_XP if rank == 1 else CONSOLATION_XP
        return Reward(coins=result.payouts[rank - 1], xp=xp)
    return consolation_reward()


def consolation_reward() -> Reward:
    return Reward(coins=CONSOLATION_COINS, xp=CONSOLATION_XP)


# ─────────────────────────────────────────────────────────────────────────────
# Daily rewards  (source: Daily Rewards screen — 7-day streak cycle)
# ─────────────────────────────────────────────────────────────────────────────


class DailyKind(str, Enum):
    COINS = "coins"
    GEMS = "gems"
    XP_MULTIPLIER = "xp_multiplier"   # temporary XP boost for the session
    MYSTERY_BOX = "mystery_box"       # rolls a loot table (resolved elsewhere)


@dataclass(frozen=True)
class DailyReward:
    day: int
    kind: DailyKind
    amount: int
    special: bool = False


DAILY_CYCLE: list[DailyReward] = [
    DailyReward(1, DailyKind.COINS, 50),
    DailyReward(2, DailyKind.COINS, 100),
    DailyReward(3, DailyKind.XP_MULTIPLIER, 5),
    DailyReward(4, DailyKind.GEMS, 5),
    DailyReward(5, DailyKind.COINS, 300),
    DailyReward(6, DailyKind.MYSTERY_BOX, 1),
    DailyReward(7, DailyKind.GEMS, 50, special=True),
]

DAILY_CYCLE_LENGTH = len(DAILY_CYCLE)


def daily_reward(streak_day: int) -> DailyReward:
    """Reward for a given day in the streak. The cycle repeats every 7 days;
    day 8 == day 1, etc. ``streak_day`` is 1-indexed."""
    if streak_day < 1:
        raise ValueError("streak_day is 1-indexed")
    return DAILY_CYCLE[(streak_day - 1) % DAILY_CYCLE_LENGTH]


# ─────────────────────────────────────────────────────────────────────────────
# Shop  (source: Shop screen — Coins / Gems / Boosters / Themes tabs)
# ─────────────────────────────────────────────────────────────────────────────


class PriceKind(str, Enum):
    USD = "usd"       # real-money IAP; value is price in USD cents
    COINS = "coins"   # bought with soft currency
    GEMS = "gems"     # bought with hard currency


@dataclass(frozen=True)
class ShopItem:
    id: str
    name: str
    emoji: str
    price_kind: PriceKind
    price: int                          # USD cents, or coins, or gems
    grants: Reward = field(default_factory=Reward)
    effect: str | None = None           # for boosters/themes: what it does
    badge: str | None = None


SHOP_COINS: list[ShopItem] = [
    ShopItem("starter_pack", "Starter Pack", "💰", PriceKind.USD, 99, Reward(coins=500)),
    ShopItem("coin_bundle", "Coin Bundle", "🪙", PriceKind.USD, 499, Reward(coins=3_000), badge="POPULAR"),
    ShopItem("mega_coins", "Mega Coins", "💫", PriceKind.USD, 999, Reward(coins=8_000), badge="BEST VALUE"),
    ShopItem("gold_rush", "Gold Rush", "🏅", PriceKind.USD, 1_999, Reward(coins=20_000)),
]

SHOP_GEMS: list[ShopItem] = [
    ShopItem("gem_starter", "Gem Starter", "💎", PriceKind.USD, 199, Reward(gems=50)),
    ShopItem("gem_pack", "Gem Pack", "✨", PriceKind.USD, 799, Reward(gems=250), badge="POPULAR"),
    ShopItem("gem_vault", "Gem Vault", "🔮", PriceKind.USD, 1_499, Reward(gems=600), badge="BEST VALUE"),
    ShopItem("diamond_box", "Diamond Box", "💍", PriceKind.USD, 2_999, Reward(gems=1_500)),
]

SHOP_BOOSTERS: list[ShopItem] = [
    ShopItem("auto_daub", "Auto-Daub ×5", "⚡", PriceKind.COINS, 200, effect="auto_mark:5_rounds"),
    ShopItem("lucky_star", "Lucky Star", "⭐", PriceKind.COINS, 500, effect="coins_multiplier:2", badge="HOT"),
    ShopItem("time_freeze", "Time Freeze", "⏰", PriceKind.GEMS, 20, effect="pause_timer:30s"),
    ShopItem("wild_number", "Wild Number", "🃏", PriceKind.GEMS, 50, effect="mark_any:1", badge="RARE"),
]

SHOP_THEMES: list[ShopItem] = [
    ShopItem("galaxy", "Galaxy", "🌌", PriceKind.GEMS, 100, effect="skin:galaxy"),
    ShopItem("neon_rush", "Neon Rush", "🌈", PriceKind.GEMS, 80, effect="skin:neon", badge="NEW"),
    ShopItem("gold_classic", "Gold Classic", "🥇", PriceKind.GEMS, 150, effect="skin:gold"),
    ShopItem("candy_land", "Candy Land", "🍭", PriceKind.GEMS, 60, effect="skin:candy"),
]

SHOP_CATALOG: dict[str, ShopItem] = {
    item.id: item
    for group in (SHOP_COINS, SHOP_GEMS, SHOP_BOOSTERS, SHOP_THEMES)
    for item in group
}


def get_shop_item(item_id: str) -> ShopItem:
    try:
        return SHOP_CATALOG[item_id]
    except KeyError:
        raise ValueError(f"unknown shop item: {item_id!r}") from None


# ─────────────────────────────────────────────────────────────────────────────
# Balances & spending
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class Balance:
    coins: int = 0
    gems: int = 0
    xp: int = 0

    def can_afford(self, currency: Currency, amount: int) -> bool:
        _nonnegative_integer(amount, "amount")
        return getattr(self, currency.value) >= amount

    def credit(self, reward: Reward) -> "Balance":
        for amount in (reward.coins, reward.gems, reward.xp):
            _nonnegative_integer(amount, "reward amount")
        self.coins += reward.coins
        self.gems += reward.gems
        self.xp += reward.xp
        return self

    def debit(self, currency: Currency, amount: int) -> "Balance":
        _nonnegative_integer(amount, "amount")
        if amount < 0:
            raise ValueError("cannot debit a negative amount")
        if not self.can_afford(currency, amount):
            raise InsufficientFunds(currency, amount, getattr(self, currency.value))
        setattr(self, currency.value, getattr(self, currency.value) - amount)
        return self


class InsufficientFunds(Exception):
    def __init__(self, currency: Currency, needed: int, available: int):
        self.currency, self.needed, self.available = currency, needed, available
        super().__init__(
            f"insufficient {currency.value}: need {needed}, have {available}"
        )


def purchase(balance: Balance, item_id: str) -> Balance:
    """Apply a shop purchase to ``balance`` in place and return it.

    Real-money (USD) items are settled by the payment provider before this is
    called; here we only grant the rewards. Coin/gem items debit the balance
    first (raising :class:`InsufficientFunds` if too poor), then grant.
    """
    item = get_shop_item(item_id)
    if item.price_kind == PriceKind.COINS:
        balance.debit(Currency.COINS, item.price)
    elif item.price_kind == PriceKind.GEMS:
        balance.debit(Currency.GEMS, item.price)
    # USD items are pre-paid; nothing to debit on-ledger.
    return balance.credit(item.grants)


def pay_entry(balance: Balance, room_id: str, num_cards: int) -> tuple[Balance, int]:
    """Debit a room entry from ``balance``. Returns (balance, cost_paid)."""
    cost = entry_cost(room_id, num_cards)
    balance.debit(Currency.COINS, cost)
    return balance, cost


# ─────────────────────────────────────────────────────────────────────────────
# XP & leveling  (source: Profile screen — "Level 24 · 7,840 / 10,000 XP")
# ─────────────────────────────────────────────────────────────────────────────

XP_BASE = 1_000       # XP to go from level 1 -> 2
XP_GROWTH = 400       # additional XP required for each subsequent level


def xp_for_next_level(level: int) -> int:
    """XP required to advance from ``level`` to ``level + 1``."""
    if level < 1:
        raise ValueError("level is 1-indexed")
    return XP_BASE + (level - 1) * XP_GROWTH


def total_xp_for_level(level: int) -> int:
    """Cumulative XP required to REACH ``level`` (level 1 == 0 XP)."""
    if level < 1:
        raise ValueError("level is 1-indexed")
    return sum(xp_for_next_level(l) for l in range(1, level))


@dataclass(frozen=True)
class LevelProgress:
    level: int
    xp_into_level: int
    xp_for_next: int


def level_from_total_xp(total_xp: int) -> LevelProgress:
    """Resolve a cumulative XP total into level + progress toward the next."""
    if total_xp < 0:
        raise ValueError("total_xp must be non-negative")
    level = 1
    while total_xp >= xp_for_next_level(level):
        total_xp -= xp_for_next_level(level)
        level += 1
    return LevelProgress(level, total_xp, xp_for_next_level(level))
