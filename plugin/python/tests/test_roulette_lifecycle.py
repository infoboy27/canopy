"""End-to-end tests of the roulette round escrow lifecycle against an
in-memory fake plugin (state_read/state_write backed by a plain dict) -- no
live Canopy node required. Covers open_roulette -> roulette_bet ->
settle_roulette (happy path across bet types + rake) and
open_roulette -> roulette_bet -> expire_roulette (abandoned-round refund).

Mirrors test_room_lifecycle.py's FakeState harness exactly.
"""

import asyncio

import pytest

from contract.contract import (
    Contract,
    ADMIN_ADDRESSES,
    ROOM_EXPIRY_BLOCKS,
    SETTLE_GRACE_BLOCKS,
    TREASURY_ADDRESS,
    seed_commitment,
    key_for_account,
    key_for_roulette_round,
    roulette_escrow_address,
    roulette_bond_address,
    rng_v2_seed,
    marshal,
    unmarshal,
)
from contract.plugin import Config
from contract.error import PluginError
from contract.proto import (
    Account,
    PluginStateReadRequest,
    PluginStateWriteRequest,
    PluginStateReadResponse,
    PluginStateWriteResponse,
    PluginReadResult,
    PluginStateEntry,
)
from contract.proto.tx_pb2 import (
    MessageOpenRoulette,
    MessageRouletteBet,
    MessageSettleRoulette,
    MessageExpireRoulette,
    RouletteRound,
)
from contract.game import roulette as groulette
from tests.test_room_lifecycle import (
    OPERATOR_BOND, fund_operator, close_round, entropy_for_close_height,
)

ADMIN = next(iter(ADMIN_ADDRESSES))
PLAYER_A = b"p" * 20
PLAYER_B = b"q" * 20
PLAYER_C = b"r" * 20

DEFAULT_RID = b"round001"
DEFAULT_OPEN_HEIGHT = 1000
DEFAULT_CLOSE_HEIGHT = DEFAULT_OPEN_HEIGHT + 1


def secret_for_spin(target_spin, round_id=DEFAULT_RID, close_height=DEFAULT_CLOSE_HEIGHT, start=0):
    """Brute-force a revealed operator secret whose fair-randomness-v2 replay
    seed -- SHA256(secret || consensus_entropy || round_id) -- lands the wheel
    on `target_spin`. The consensus entropy is fixed by `close_round`, so this
    can be computed before the round even opens."""
    entropy = entropy_for_close_height(close_height)
    for i in range(start, start + 5_000_000):
        secret = f"seed-{i}".encode().ljust(32, b"\x00")
        if groulette.spin_number(rng_v2_seed(secret, entropy, round_id)) == target_spin:
            return secret
    raise AssertionError(f"no secret found for spin {target_spin}")


SEED_FOR_1 = secret_for_spin(1)   # red, odd, low, dozen1, col1
SEED_FOR_0 = secret_for_spin(0)   # green -- every outside bet loses


def close_roulette(contract, round_id=DEFAULT_RID, **kw):
    return close_round(contract, round_id, key_for_roulette_round(round_id), RouletteRound, **kw)


class FakeState:
    def __init__(self):
        self.kv = {}

    async def state_read(self, contract, request: PluginStateReadRequest) -> PluginStateReadResponse:
        resp = PluginStateReadResponse()
        for kr in request.keys:
            result = PluginReadResult(query_id=kr.query_id)
            val = self.kv.get(bytes(kr.key))
            if val is not None:
                result.entries.append(PluginStateEntry(key=kr.key, value=val))
            resp.results.append(result)
        return resp

    async def state_write(self, contract, request: PluginStateWriteRequest) -> PluginStateWriteResponse:
        for op in request.sets:
            self.kv[bytes(op.key)] = op.value
        for op in request.deletes:
            self.kv.pop(bytes(op.key), None)
        return PluginStateWriteResponse()

    def set_balance(self, address: bytes, amount: int) -> None:
        self.kv[key_for_account(address)] = marshal(Account(amount=amount))

    def balance(self, address: bytes) -> int:
        val = self.kv.get(key_for_account(address))
        return unmarshal(Account, val).amount if val else 0


@pytest.fixture
def state():
    return FakeState()


@pytest.fixture
def contract(state):
    return Contract(config=Config(), plugin=state)


def run(coro):
    return asyncio.run(coro)


def open_roulette(contract, round_id=DEFAULT_RID, rake_bps=1000, height=DEFAULT_OPEN_HEIGHT,
                   commitment=b"c" * 32, operator=ADMIN, operator_bond=OPERATOR_BOND):
    if operator == ADMIN:
        fund_operator(contract.plugin, operator_bond)
    msg = MessageOpenRoulette(operator_address=operator, round_id=round_id,
                               commitment=commitment, rake_bps=rake_bps,
                               operator_bond=operator_bond)
    resp = run(contract._deliver_message_open_roulette(msg, height))
    assert not resp.HasField("error"), resp.error.msg
    return round_id


def place_bet(contract, state, player, round_id, amount, bet_type, bet_number=0):
    reserve = groulette.liability_reserve(bet_type, amount)
    if player != TREASURY_ADDRESS and state.balance(TREASURY_ADDRESS) < reserve:
        state.set_balance(TREASURY_ADDRESS, reserve)
    state.set_balance(player, amount)
    msg = MessageRouletteBet(player_address=player, round_id=round_id, bet_type=bet_type,
                              bet_number=bet_number, amount=amount)
    resp = run(contract._deliver_message_roulette_bet(msg))
    assert not resp.HasField("error"), resp.error.msg


class TestOpenRoulette:
    def test_non_admin_cannot_open(self, contract, state):
        # The admin gate lives in check_tx (statelessly, before any state is
        # touched) -- deliver_tx trusts that check_tx already ran, exactly
        # like every other message in this contract, so this exercises
        # _check_message_open_roulette directly rather than the deliver path.
        impostor = b"x" * 20
        msg = MessageOpenRoulette(operator_address=impostor, round_id=b"round001",
                                   commitment=b"c" * 32, rake_bps=1000)
        with pytest.raises(PluginError):
            contract._check_message_open_roulette(msg)


class TestSettleRouletteHappyPath:
    def test_treasury_backstops_a_big_win_beyond_this_rounds_pot(self, contract, state):
        # A 36x straight-up win routinely exceeds what one round collected --
        # unlike Bingo's pari-mutuel split, the payout isn't bounded by the
        # pot. The accumulated treasury (rake + house_take from other rounds)
        # must cover the shortfall, like a real casino bankroll.
        state.set_balance(TREASURY_ADDRESS, 10_000)
        rid = open_roulette(contract, rake_bps=1000, commitment=seed_commitment(SEED_FOR_1))
        place_bet(contract, state, PLAYER_A, rid, 100, "straight", 1)  # wins 36x = 3600
        place_bet(contract, state, PLAYER_B, rid, 100, "red")           # wins 2x = 200
        place_bet(contract, state, PLAYER_C, rid, 100, "black")         # loses
        settle_height, _ = close_roulette(contract, rid)

        resp = run(contract._deliver_message_settle_roulette(
            MessageSettleRoulette(operator_address=ADMIN, round_id=rid, seed=SEED_FOR_1),
            settle_height))
        assert not resp.HasField("error"), resp.error.msg

        assert state.balance(PLAYER_A) == 3240  # gross 3600, 10% rake = 360, net 3240
        assert state.balance(PLAYER_B) == 180    # gross 200, 10% rake = 20, net 180
        assert state.balance(PLAYER_C) == 0      # lost, stake absorbed as house profit

        assert state.balance(roulette_escrow_address(rid)) == 0
        # pot=300 covers 300 of the 3800 gross owed; treasury fronts the
        # 3500 shortfall, net rake collected (380) offsets it:
        # 10000 - 3500 + 380 = 6880
        assert state.balance(TREASURY_ADDRESS) == 6880

    def test_bet_fails_before_acceptance_when_liability_cannot_be_reserved(self, contract, state):
        # Treasury starts empty. The bet itself must fail atomically instead of
        # creating a round that can only discover insolvency at settlement.
        rid = open_roulette(contract, rake_bps=1000, commitment=seed_commitment(SEED_FOR_1))
        state.set_balance(PLAYER_A, 100)
        before = dict(state.kv)
        with pytest.raises(PluginError, match="insufficient funds"):
            run(contract._deliver_message_roulette_bet(MessageRouletteBet(
                player_address=PLAYER_A, round_id=rid, bet_type="straight", bet_number=1, amount=100)))
        assert state.kv == before

    def test_realistic_pot_house_take_goes_to_treasury(self, contract, state):
        rid = open_roulette(contract, rake_bps=1000, commitment=seed_commitment(SEED_FOR_1))
        place_bet(contract, state, PLAYER_A, rid, 100, "red")   # wins 2x = 200
        place_bet(contract, state, PLAYER_B, rid, 100, "black")  # loses
        place_bet(contract, state, PLAYER_C, rid, 100, "black")  # loses
        settle_height, _ = close_roulette(contract, rid)

        resp = run(contract._deliver_message_settle_roulette(
            MessageSettleRoulette(operator_address=ADMIN, round_id=rid, seed=SEED_FOR_1),
            settle_height))
        assert not resp.HasField("error"), resp.error.msg

        # pot = 300. Winner A: gross 200, rake 20, net 180.
        assert state.balance(PLAYER_A) == 180
        assert state.balance(PLAYER_B) == 0
        assert state.balance(PLAYER_C) == 0
        # escrow fully drained; treasury gets rake (20) + house_take
        # (pot 300 - gross_to_winners 200 = 100) = 120
        assert state.balance(roulette_escrow_address(rid)) == 0
        assert state.balance(TREASURY_ADDRESS) == 420  # initial 300 reserve + 120 house result

    def test_zero_loses_every_outside_bet(self, contract, state):
        rid = open_roulette(contract, rake_bps=0, commitment=seed_commitment(SEED_FOR_0))
        place_bet(contract, state, PLAYER_A, rid, 100, "red")
        place_bet(contract, state, PLAYER_B, rid, 100, "black")
        place_bet(contract, state, PLAYER_C, rid, 100, "even")
        settle_height, _ = close_roulette(contract, rid)

        resp = run(contract._deliver_message_settle_roulette(
            MessageSettleRoulette(operator_address=ADMIN, round_id=rid, seed=SEED_FOR_0),
            settle_height))
        assert not resp.HasField("error"), resp.error.msg

        assert state.balance(PLAYER_A) == 0
        assert state.balance(PLAYER_B) == 0
        assert state.balance(PLAYER_C) == 0
        assert state.balance(TREASURY_ADDRESS) == 600  # initial 300 reserve + entire 300 pot

    def test_wrong_seed_rejected(self, contract, state):
        rid = open_roulette(contract, commitment=seed_commitment(SEED_FOR_1))
        place_bet(contract, state, PLAYER_A, rid, 100, "red")
        with pytest.raises(PluginError, match="commitment"):
            run(contract._deliver_message_settle_roulette(
                MessageSettleRoulette(operator_address=ADMIN, round_id=rid,
                                       seed=b"wrong seed" + b"\x00" * 22)))

    def test_non_operator_cannot_settle(self, contract, state):
        rid = open_roulette(contract, commitment=seed_commitment(SEED_FOR_1))
        place_bet(contract, state, PLAYER_A, rid, 100, "red")
        impostor = b"x" * 20
        with pytest.raises(PluginError, match="only the operator"):
            run(contract._deliver_message_settle_roulette(
                MessageSettleRoulette(operator_address=impostor, round_id=rid, seed=SEED_FOR_1)))

    def test_double_bet_from_same_address_rejected(self, contract, state):
        rid = open_roulette(contract, commitment=seed_commitment(SEED_FOR_1))
        place_bet(contract, state, PLAYER_A, rid, 100, "red")
        state.set_balance(PLAYER_A, 100)
        with pytest.raises(PluginError, match="already has a bet"):
            run(contract._deliver_message_roulette_bet(
                MessageRouletteBet(player_address=PLAYER_A, round_id=rid, bet_type="black",
                                    bet_number=0, amount=100)))


class TestExpireRoulette:
    def test_refund_before_deadline_rejected(self, contract, state):
        rid = open_roulette(contract, height=1000)
        place_bet(contract, state, PLAYER_A, rid, 100, "red")

        with pytest.raises(PluginError) as exc:
            run(contract._deliver_message_expire_roulette(
                MessageExpireRoulette(caller_address=PLAYER_A, round_id=rid),
                height=1000 + ROOM_EXPIRY_BLOCKS - 1,
            ))
        assert exc.value.code == 16  # err_room_not_expired

    def test_refunds_every_bettor_after_deadline(self, contract, state):
        rid = open_roulette(contract, height=1000)
        place_bet(contract, state, PLAYER_A, rid, 100, "red")
        place_bet(contract, state, PLAYER_B, rid, 150, "straight", 1)

        assert state.balance(roulette_escrow_address(rid)) == 5600  # stakes + worst-case reserves
        assert state.balance(PLAYER_A) == 0
        assert state.balance(PLAYER_B) == 0

        caller = b"z" * 20  # anyone may call this
        resp = run(contract._deliver_message_expire_roulette(
            MessageExpireRoulette(caller_address=caller, round_id=rid),
            height=1000 + ROOM_EXPIRY_BLOCKS,
        ))
        assert not resp.HasField("error"), resp.error.msg

        assert state.balance(PLAYER_A) == 100
        assert state.balance(PLAYER_B) == 150
        assert state.balance(roulette_escrow_address(rid)) == 0
        # 5350 released reserves + the slashed 500 operator bond
        assert state.balance(roulette_bond_address(rid)) == 0
        assert state.balance(TREASURY_ADDRESS) == 5850

    def test_cannot_expire_twice(self, contract, state):
        rid = open_roulette(contract, height=1000)
        place_bet(contract, state, PLAYER_A, rid, 100, "red")
        expire_height = 1000 + ROOM_EXPIRY_BLOCKS
        run(contract._deliver_message_expire_roulette(
            MessageExpireRoulette(caller_address=PLAYER_A, round_id=rid), height=expire_height))

        with pytest.raises(PluginError, match="not open"):
            run(contract._deliver_message_expire_roulette(
                MessageExpireRoulette(caller_address=PLAYER_A, round_id=rid), height=expire_height + 1))

    def test_settled_round_cannot_be_expired(self, contract, state):
        rid = open_roulette(contract, rake_bps=0, height=1000, commitment=seed_commitment(SEED_FOR_1))
        place_bet(contract, state, PLAYER_A, rid, 100, "black")  # loses on spin=1 (red)
        settle_height, _ = close_roulette(contract, rid)
        run(contract._deliver_message_settle_roulette(
            MessageSettleRoulette(operator_address=ADMIN, round_id=rid, seed=SEED_FOR_1),
            settle_height))

        with pytest.raises(PluginError, match="not open"):
            run(contract._deliver_message_expire_roulette(
                MessageExpireRoulette(caller_address=PLAYER_A, round_id=rid),
                height=settle_height + ROOM_EXPIRY_BLOCKS + SETTLE_GRACE_BLOCKS,
            ))

    def test_unknown_round_rejected(self, contract, state):
        with pytest.raises(PluginError, match="not found"):
            run(contract._deliver_message_expire_roulette(
                MessageExpireRoulette(caller_address=PLAYER_A, round_id=b"nope"),
                height=999_999,
            ))
