"""End-to-end tests of the poker round escrow lifecycle against an
in-memory fake plugin (state_read/state_write backed by a plain dict) -- no
live Canopy node required. Covers open_poker -> join_poker (x2) ->
settle_poker (showdown happy path + fold happy path + rejected illegal
action claims + rake) and open_poker -> join_poker -> expire_poker
(abandoned-round refund).

Mirrors test_domino_lifecycle.py's FakeState harness exactly.
"""

import asyncio

import pytest

from contract.contract import (
    Contract,
    ADMIN_ADDRESSES,
    ROOM_EXPIRY_BLOCKS,
    TREASURY_ADDRESS,
    seed_commitment,
    key_for_account,
    poker_escrow_address,
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
    MessageOpenPoker,
    MessageJoinPoker,
    MessageSettlePoker,
    MessageExpirePoker,
    PokerActionRecord,
)
from contract.game import poker as gpoker

ADMIN = next(iter(ADMIN_ADDRESSES))
PLAYER_A = b"p" * 20
PLAYER_B = b"q" * 20

SMALL_BLIND = 10
BIG_BLIND = 20
BUY_IN = 1000
RAKE_BPS = 1000  # 10%


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


def open_poker(contract, round_id=b"round001", small_blind=SMALL_BLIND, big_blind=BIG_BLIND,
               buy_in=BUY_IN, rake_bps=RAKE_BPS, height=1000, commitment=b"c" * 32, operator=ADMIN):
    msg = MessageOpenPoker(operator_address=operator, round_id=round_id, commitment=commitment,
                           small_blind=small_blind, big_blind=big_blind, buy_in=buy_in, rake_bps=rake_bps)
    resp = run(contract._deliver_message_open_poker(msg, height))
    assert not resp.HasField("error"), resp.error.msg
    return round_id


def join_poker(contract, state, player, round_id, amount):
    state.set_balance(player, amount)
    msg = MessageJoinPoker(player_address=player, round_id=round_id, amount=amount)
    resp = run(contract._deliver_message_join_poker(msg))
    assert not resp.HasField("error"), resp.error.msg


def actions_to_proto(actions):
    return [PokerActionRecord(action=a.action, amount=a.amount) for a in actions]


SEED_CHECKDOWN = b"s" * 32
CHECKDOWN_ACTIONS = [gpoker.PokerAction("check_call")] * 8
RESULT_CHECKDOWN = gpoker.replay(SEED_CHECKDOWN, SMALL_BLIND, BIG_BLIND, (BUY_IN, BUY_IN), CHECKDOWN_ACTIONS)
PROTO_CHECKDOWN_ACTIONS = actions_to_proto(CHECKDOWN_ACTIONS)

SEED_FOLD = b"f" * 32
FOLD_ACTIONS = [gpoker.PokerAction("fold")]
RESULT_FOLD = gpoker.replay(SEED_FOLD, SMALL_BLIND, BIG_BLIND, (BUY_IN, BUY_IN), FOLD_ACTIONS)
PROTO_FOLD_ACTIONS = actions_to_proto(FOLD_ACTIONS)


class TestOpenPoker:
    def test_non_admin_cannot_open(self, contract, state):
        impostor = b"x" * 20
        msg = MessageOpenPoker(operator_address=impostor, round_id=b"round001", commitment=b"c" * 32,
                               small_blind=SMALL_BLIND, big_blind=BIG_BLIND, buy_in=BUY_IN, rake_bps=RAKE_BPS)
        with pytest.raises(PluginError):
            contract._check_message_open_poker(msg)


class TestJoinPoker:
    def test_third_join_rejected(self, contract, state):
        rid = open_poker(contract)
        join_poker(contract, state, PLAYER_A, rid, BUY_IN)
        join_poker(contract, state, PLAYER_B, rid, BUY_IN)
        third = b"z" * 20
        state.set_balance(third, BUY_IN)
        with pytest.raises(PluginError, match="enough players"):
            run(contract._deliver_message_join_poker(
                MessageJoinPoker(player_address=third, round_id=rid, amount=BUY_IN)))

    def test_same_address_cannot_join_twice(self, contract, state):
        rid = open_poker(contract)
        join_poker(contract, state, PLAYER_A, rid, BUY_IN)
        state.set_balance(PLAYER_A, BUY_IN)
        with pytest.raises(PluginError, match="already joined"):
            run(contract._deliver_message_join_poker(
                MessageJoinPoker(player_address=PLAYER_A, round_id=rid, amount=BUY_IN)))

    def test_wrong_amount_rejected(self, contract, state):
        rid = open_poker(contract)
        state.set_balance(PLAYER_A, 999)
        with pytest.raises(PluginError, match="buy_in"):
            run(contract._deliver_message_join_poker(
                MessageJoinPoker(player_address=PLAYER_A, round_id=rid, amount=999)))


class TestSettlePokerHappyPath:
    def test_winner_takes_the_pot_net_of_rake_at_showdown(self, contract, state):
        rid = open_poker(contract, commitment=seed_commitment(SEED_CHECKDOWN))
        join_poker(contract, state, PLAYER_A, rid, BUY_IN)
        join_poker(contract, state, PLAYER_B, rid, BUY_IN)

        resp = run(contract._deliver_message_settle_poker(
            MessageSettlePoker(operator_address=ADMIN, round_id=rid, seed=SEED_CHECKDOWN,
                               actions=PROTO_CHECKDOWN_ACTIONS)))
        assert not resp.HasField("error"), resp.error.msg

        winner_slot = RESULT_CHECKDOWN.winners[0]
        winner_addr = [PLAYER_A, PLAYER_B][winner_slot]
        loser_addr = [PLAYER_A, PLAYER_B][1 - winner_slot]
        rake = RESULT_CHECKDOWN.pot * RAKE_BPS // 10000
        net_pot = RESULT_CHECKDOWN.pot - rake
        assert state.balance(winner_addr) == RESULT_CHECKDOWN.stacks_remaining[winner_slot] + net_pot
        assert state.balance(loser_addr) == RESULT_CHECKDOWN.stacks_remaining[1 - winner_slot]
        assert state.balance(TREASURY_ADDRESS) == rake
        assert state.balance(poker_escrow_address(rid)) == 0

    def test_fold_awards_the_whole_pot_to_the_other_player(self, contract, state):
        rid = open_poker(contract, commitment=seed_commitment(SEED_FOLD))
        join_poker(contract, state, PLAYER_A, rid, BUY_IN)
        join_poker(contract, state, PLAYER_B, rid, BUY_IN)

        resp = run(contract._deliver_message_settle_poker(
            MessageSettlePoker(operator_address=ADMIN, round_id=rid, seed=SEED_FOLD,
                               actions=PROTO_FOLD_ACTIONS)))
        assert not resp.HasField("error"), resp.error.msg

        winner_slot = RESULT_FOLD.winners[0]
        assert RESULT_FOLD.reason == "fold"
        winner_addr = [PLAYER_A, PLAYER_B][winner_slot]
        loser_addr = [PLAYER_A, PLAYER_B][1 - winner_slot]
        rake = RESULT_FOLD.pot * RAKE_BPS // 10000
        net_pot = RESULT_FOLD.pot - rake
        assert state.balance(winner_addr) == RESULT_FOLD.stacks_remaining[winner_slot] + net_pot
        assert state.balance(loser_addr) == RESULT_FOLD.stacks_remaining[1 - winner_slot]
        assert state.balance(TREASURY_ADDRESS) == rake
        assert state.balance(poker_escrow_address(rid)) == 0

    def test_wrong_seed_rejected(self, contract, state):
        rid = open_poker(contract, commitment=seed_commitment(SEED_CHECKDOWN))
        join_poker(contract, state, PLAYER_A, rid, BUY_IN)
        join_poker(contract, state, PLAYER_B, rid, BUY_IN)
        with pytest.raises(PluginError, match="commitment"):
            run(contract._deliver_message_settle_poker(
                MessageSettlePoker(operator_address=ADMIN, round_id=rid,
                                   seed=b"wrong seed" + b"\x00" * 22, actions=PROTO_CHECKDOWN_ACTIONS)))

    def test_non_operator_cannot_settle(self, contract, state):
        rid = open_poker(contract, commitment=seed_commitment(SEED_CHECKDOWN))
        join_poker(contract, state, PLAYER_A, rid, BUY_IN)
        join_poker(contract, state, PLAYER_B, rid, BUY_IN)
        impostor = b"x" * 20
        with pytest.raises(PluginError, match="only the operator"):
            run(contract._deliver_message_settle_poker(
                MessageSettlePoker(operator_address=impostor, round_id=rid, seed=SEED_CHECKDOWN,
                                   actions=PROTO_CHECKDOWN_ACTIONS)))

    def test_settle_before_both_seats_filled_rejected(self, contract, state):
        rid = open_poker(contract, commitment=seed_commitment(SEED_CHECKDOWN))
        join_poker(contract, state, PLAYER_A, rid, BUY_IN)
        with pytest.raises(PluginError, match="never filled both seats"):
            run(contract._deliver_message_settle_poker(
                MessageSettlePoker(operator_address=ADMIN, round_id=rid, seed=SEED_CHECKDOWN,
                                   actions=PROTO_CHECKDOWN_ACTIONS)))

    def test_claimed_illegal_action_rejected(self, contract, state):
        rid = open_poker(contract, commitment=seed_commitment(SEED_CHECKDOWN))
        join_poker(contract, state, PLAYER_A, rid, BUY_IN)
        join_poker(contract, state, PLAYER_B, rid, BUY_IN)
        # a raise-to of only BB+1 as the very first action -- below the legal minimum
        bogus_actions = [PokerActionRecord(action="bet_raise", amount=BIG_BLIND + 1)]
        with pytest.raises(PluginError, match="illegal action"):
            run(contract._deliver_message_settle_poker(
                MessageSettlePoker(operator_address=ADMIN, round_id=rid, seed=SEED_CHECKDOWN,
                                   actions=bogus_actions)))

    def test_cannot_settle_twice(self, contract, state):
        rid = open_poker(contract, commitment=seed_commitment(SEED_CHECKDOWN))
        join_poker(contract, state, PLAYER_A, rid, BUY_IN)
        join_poker(contract, state, PLAYER_B, rid, BUY_IN)
        run(contract._deliver_message_settle_poker(
            MessageSettlePoker(operator_address=ADMIN, round_id=rid, seed=SEED_CHECKDOWN,
                               actions=PROTO_CHECKDOWN_ACTIONS)))
        with pytest.raises(PluginError, match="already settled"):
            run(contract._deliver_message_settle_poker(
                MessageSettlePoker(operator_address=ADMIN, round_id=rid, seed=SEED_CHECKDOWN,
                                   actions=PROTO_CHECKDOWN_ACTIONS)))


class TestExpirePoker:
    def test_refund_before_deadline_rejected(self, contract, state):
        rid = open_poker(contract, height=1000)
        join_poker(contract, state, PLAYER_A, rid, BUY_IN)

        with pytest.raises(PluginError) as exc:
            run(contract._deliver_message_expire_poker(
                MessageExpirePoker(caller_address=PLAYER_A, round_id=rid),
                height=1000 + ROOM_EXPIRY_BLOCKS - 1,
            ))
        assert exc.value.code == 16  # err_room_not_expired

    def test_refunds_every_participant_after_deadline(self, contract, state):
        rid = open_poker(contract, height=1000)
        join_poker(contract, state, PLAYER_A, rid, BUY_IN)
        join_poker(contract, state, PLAYER_B, rid, BUY_IN)

        assert state.balance(poker_escrow_address(rid)) == BUY_IN * 2
        caller = b"z" * 20  # anyone may call this
        resp = run(contract._deliver_message_expire_poker(
            MessageExpirePoker(caller_address=caller, round_id=rid),
            height=1000 + ROOM_EXPIRY_BLOCKS,
        ))
        assert not resp.HasField("error"), resp.error.msg

        assert state.balance(PLAYER_A) == BUY_IN
        assert state.balance(PLAYER_B) == BUY_IN
        assert state.balance(poker_escrow_address(rid)) == 0

    def test_refunds_a_lone_participant_if_second_seat_never_filled(self, contract, state):
        rid = open_poker(contract, height=1000)
        join_poker(contract, state, PLAYER_A, rid, BUY_IN)

        resp = run(contract._deliver_message_expire_poker(
            MessageExpirePoker(caller_address=PLAYER_A, round_id=rid),
            height=1000 + ROOM_EXPIRY_BLOCKS,
        ))
        assert not resp.HasField("error"), resp.error.msg
        assert state.balance(PLAYER_A) == BUY_IN

    def test_cannot_expire_twice(self, contract, state):
        rid = open_poker(contract, height=1000)
        join_poker(contract, state, PLAYER_A, rid, BUY_IN)
        expire_height = 1000 + ROOM_EXPIRY_BLOCKS
        run(contract._deliver_message_expire_poker(
            MessageExpirePoker(caller_address=PLAYER_A, round_id=rid), height=expire_height))

        with pytest.raises(PluginError, match="not open"):
            run(contract._deliver_message_expire_poker(
                MessageExpirePoker(caller_address=PLAYER_A, round_id=rid), height=expire_height + 1))

    def test_settled_round_cannot_be_expired(self, contract, state):
        rid = open_poker(contract, rake_bps=0, height=1000, commitment=seed_commitment(SEED_CHECKDOWN))
        join_poker(contract, state, PLAYER_A, rid, BUY_IN)
        join_poker(contract, state, PLAYER_B, rid, BUY_IN)
        run(contract._deliver_message_settle_poker(
            MessageSettlePoker(operator_address=ADMIN, round_id=rid, seed=SEED_CHECKDOWN,
                               actions=PROTO_CHECKDOWN_ACTIONS)))

        with pytest.raises(PluginError, match="not open"):
            run(contract._deliver_message_expire_poker(
                MessageExpirePoker(caller_address=PLAYER_A, round_id=rid),
                height=1000 + ROOM_EXPIRY_BLOCKS,
            ))

    def test_unknown_round_rejected(self, contract, state):
        with pytest.raises(PluginError, match="not found"):
            run(contract._deliver_message_expire_poker(
                MessageExpirePoker(caller_address=PLAYER_A, round_id=b"nope"),
                height=999_999,
            ))
