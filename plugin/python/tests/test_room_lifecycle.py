"""End-to-end tests of the room escrow lifecycle against an in-memory fake
plugin (state_read/state_write backed by a plain dict) -- no live Canopy node
required. Covers open_room -> join_room -> settle_room (happy path + rake)
and open_room -> join_room -> expire_room (the abandoned-room refund path
added to close the "operator never settles = funds stuck forever" gap).

Before this file, NONE of open_room/join_room/settle_room/expire_room had
any test coverage -- only the pure math in test_economy.py/test_engine.py
and the stateless checks in test_contract.py were tested. `_deliver_*`
methods raise PluginError directly (deliver_tx's try/except converts that to
a response) -- called directly as here, error cases surface as exceptions.
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
    key_for_round,
    escrow_address,
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
    MessageOpenRoom,
    MessageJoinRoom,
    MessageSettleRoom,
    MessageExpireRoom,
)

ADMIN = next(iter(ADMIN_ADDRESSES))
PLAYER_A = b"p" * 20
PLAYER_B = b"q" * 20
SEED = b"s" * 32


class FakeState:
    """In-memory key/value store standing in for the real Canopy FSM state,
    wired up exactly like the plugin.state_read/state_write RPC contract."""

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
    return asyncio.get_event_loop().run_until_complete(coro)


def open_room(contract, round_id=b"round001", entry_fee=100, rake_bps=1000, height=1000,
              commitment=b"c" * 32):
    msg = MessageOpenRoom(operator_address=ADMIN, round_id=round_id, commitment=commitment,
                          entry_fee=entry_fee, rake_bps=rake_bps, payout_weights_bps=[10000])
    resp = run(contract._deliver_message_open_room(msg, height))
    assert not resp.HasField("error"), resp.error.msg
    return round_id


def join_room(contract, state, player, round_id, amount, num_cards=1):
    state.set_balance(player, amount)
    msg = MessageJoinRoom(player_address=player, round_id=round_id, num_cards=num_cards, amount=amount)
    resp = run(contract._deliver_message_join_room(msg))
    assert not resp.HasField("error"), resp.error.msg


class TestSettleRoomHappyPath:
    def test_rake_goes_to_treasury_and_net_to_winner(self, contract, state):
        rid = open_room(contract, entry_fee=100, rake_bps=1000, commitment=seed_commitment(SEED))
        join_room(contract, state, PLAYER_A, rid, 100)

        resp = run(contract._deliver_message_settle_room(
            MessageSettleRoom(operator_address=ADMIN, round_id=rid, seed=SEED, pattern="line")))
        assert not resp.HasField("error"), resp.error.msg

        # 100 escrowed, 10% rake = 10 to treasury, 90 net to the (only) winner
        assert state.balance(TREASURY_ADDRESS) == 10
        assert state.balance(PLAYER_A) == 90
        assert state.balance(escrow_address(rid)) == 0

    def test_wrong_seed_rejected(self, contract, state):
        rid = open_room(contract, commitment=seed_commitment(SEED))
        join_room(contract, state, PLAYER_A, rid, 100)
        with pytest.raises(PluginError, match="commitment"):
            run(contract._deliver_message_settle_room(
                MessageSettleRoom(operator_address=ADMIN, round_id=rid,
                                  seed=b"wrong seed" + b"\x00" * 22, pattern="line")))

    def test_non_operator_cannot_settle(self, contract, state):
        rid = open_room(contract, commitment=seed_commitment(SEED))
        join_room(contract, state, PLAYER_A, rid, 100)
        impostor = b"x" * 20
        with pytest.raises(PluginError, match="only the operator"):
            run(contract._deliver_message_settle_room(
                MessageSettleRoom(operator_address=impostor, round_id=rid, seed=SEED, pattern="line")))


class TestExpireRoom:
    """The refund path for a room the operator never settles."""

    def test_refund_before_deadline_rejected(self, contract, state):
        rid = open_room(contract, entry_fee=100, height=1000)
        join_room(contract, state, PLAYER_A, rid, 100)

        with pytest.raises(PluginError) as exc:
            run(contract._deliver_message_expire_room(
                MessageExpireRoom(caller_address=PLAYER_A, round_id=rid),
                height=1000 + ROOM_EXPIRY_BLOCKS - 1,
            ))
        assert exc.value.code == 16  # err_room_not_expired

    def test_refunds_every_participant_after_deadline(self, contract, state):
        rid = open_room(contract, entry_fee=100, height=1000)
        join_room(contract, state, PLAYER_A, rid, 100)
        join_room(contract, state, PLAYER_B, rid, 100, num_cards=2)

        assert state.balance(escrow_address(rid)) == 200
        assert state.balance(PLAYER_A) == 0  # spent joining
        assert state.balance(PLAYER_B) == 0

        # ANYONE can call this -- caller here is a third, uninvolved address.
        caller = b"z" * 20
        resp = run(contract._deliver_message_expire_room(
            MessageExpireRoom(caller_address=caller, round_id=rid),
            height=1000 + ROOM_EXPIRY_BLOCKS,
        ))
        assert not resp.HasField("error"), resp.error.msg

        assert state.balance(PLAYER_A) == 100
        assert state.balance(PLAYER_B) == 100
        assert state.balance(escrow_address(rid)) == 0

    def test_cannot_expire_twice(self, contract, state):
        rid = open_room(contract, entry_fee=100, height=1000)
        join_room(contract, state, PLAYER_A, rid, 100)
        expire_height = 1000 + ROOM_EXPIRY_BLOCKS
        run(contract._deliver_message_expire_room(
            MessageExpireRoom(caller_address=PLAYER_A, round_id=rid), height=expire_height))

        with pytest.raises(PluginError, match="not open"):
            run(contract._deliver_message_expire_room(
                MessageExpireRoom(caller_address=PLAYER_A, round_id=rid), height=expire_height + 1))

    def test_settled_room_cannot_be_expired(self, contract, state):
        rid = open_room(contract, entry_fee=100, rake_bps=0, height=1000,
                        commitment=seed_commitment(SEED))
        join_room(contract, state, PLAYER_A, rid, 100)
        run(contract._deliver_message_settle_room(
            MessageSettleRoom(operator_address=ADMIN, round_id=rid, seed=SEED, pattern="line")))

        with pytest.raises(PluginError, match="not open"):
            run(contract._deliver_message_expire_room(
                MessageExpireRoom(caller_address=PLAYER_A, round_id=rid),
                height=1000 + ROOM_EXPIRY_BLOCKS,
            ))

    def test_unknown_round_rejected(self, contract, state):
        with pytest.raises(PluginError, match="not found"):
            run(contract._deliver_message_expire_room(
                MessageExpireRoom(caller_address=PLAYER_A, round_id=b"nope"),
                height=999_999,
            ))
