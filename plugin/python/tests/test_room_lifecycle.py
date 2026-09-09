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
    SETTLE_GRACE_BLOCKS,
    ENTROPY_DELAY_BLOCKS,
    ENTROPY_WINDOW_BLOCKS,
    TREASURY_ADDRESS,
    seed_commitment,
    key_for_account,
    key_for_round,
    key_for_consensus_entropy,
    escrow_address,
    bingo_bond_address,
    fold_consensus_entropy,
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
    MessageOpenRoom,
    MessageCloseRoom,
    MessageJoinRoom,
    MessageSettleRoom,
    MessageExpireRoom,
    RoomRound,
)

ADMIN = next(iter(ADMIN_ADDRESSES))
PLAYER_A = b"p" * 20
PLAYER_B = b"q" * 20
SEED = b"s" * 32
OPERATOR_BOND = 500


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
    return asyncio.run(coro)


def fund_operator(state, extra=OPERATOR_BOND):
    """The operator must be able to escrow its fair-randomness-v2 bond at open."""
    state.set_balance(ADMIN, state.balance(ADMIN) + extra)


def open_room(contract, round_id=b"round001", entry_fee=100, rake_bps=1000, height=1000,
              commitment=b"c" * 32, operator_bond=OPERATOR_BOND):
    fund_operator(contract.plugin, operator_bond)
    msg = MessageOpenRoom(operator_address=ADMIN, round_id=round_id, commitment=commitment,
                          entry_fee=entry_fee, rake_bps=rake_bps, payout_weights_bps=[10000],
                          operator_bond=operator_bond)
    resp = run(contract._deliver_message_open_room(msg, height))
    assert not resp.HasField("error"), resp.error.msg
    return round_id


def _window_values(start, end):
    """The finalized block hashes a test injects into the entropy ledger. Fixed
    and deterministic so a test can compute the outcome ahead of settle."""
    return [bytes([(h % 254) + 1]) * 32 for h in range(start, end + 1)]


def window_for_close_height(close_height):
    start = close_height + ENTROPY_DELAY_BLOCKS
    return start, start + ENTROPY_WINDOW_BLOCKS - 1


def entropy_for_close_height(close_height):
    """The consensus entropy `close_round` will arm for a round closed at this
    height -- lets a test brute-force a secret for a target outcome before it
    even opens the round."""
    start, end = window_for_close_height(close_height)
    return fold_consensus_entropy(_window_values(start, end))


def close_round(contract, round_id, round_key, round_type, operator=ADMIN, close_height=None):
    """Close a round and arm its consensus-entropy window. Returns
    (settle_height, final_seed) -- final_seed is a callable mapping the revealed
    operator secret to the seed the plugin replays the outcome from."""
    state = contract.plugin
    rr = round_type(); rr.ParseFromString(state.kv[round_key])
    if close_height is None:
        close_height = (rr.opened_height or 0) + 1
    resp = run(contract._deliver_close_round(
        round_key, round_type,
        MessageCloseRoom(operator_address=operator, round_id=round_id), close_height))
    assert not resp.HasField("error"), resp.error.msg
    rr.ParseFromString(state.kv[round_key])
    values = _window_values(rr.entropy_start, rr.entropy_end)
    for h, v in zip(range(rr.entropy_start, rr.entropy_end + 1), values):
        state.kv[key_for_consensus_entropy(h)] = v
    entropy = fold_consensus_entropy(values)
    return rr.entropy_end + 1, (lambda secret: rng_v2_seed(secret, entropy, round_id))


def close_room(contract, round_id, **kw):
    return close_round(contract, round_id, key_for_round(round_id), RoomRound, **kw)


def join_room(contract, state, player, round_id, amount, num_cards=1):
    state.set_balance(player, amount)
    msg = MessageJoinRoom(player_address=player, round_id=round_id, num_cards=num_cards, amount=amount)
    resp = run(contract._deliver_message_join_room(msg))
    assert not resp.HasField("error"), resp.error.msg


class TestSettleRoomHappyPath:
    def test_rake_goes_to_treasury_and_net_to_winner(self, contract, state):
        rid = open_room(contract, entry_fee=100, rake_bps=1000, commitment=seed_commitment(SEED))
        join_room(contract, state, PLAYER_A, rid, 100)
        settle_height, _ = close_room(contract, rid)

        resp = run(contract._deliver_message_settle_room(
            MessageSettleRoom(operator_address=ADMIN, round_id=rid, seed=SEED, pattern="line"),
            settle_height))
        assert not resp.HasField("error"), resp.error.msg

        # 100 escrowed, 10% rake = 10 to treasury, 90 net to the (only) winner
        assert state.balance(TREASURY_ADDRESS) == 10
        assert state.balance(PLAYER_A) == 90
        assert state.balance(escrow_address(rid)) == 0
        # the operator bond returns from its sub-account to the operator
        assert state.balance(bingo_bond_address(rid)) == 0
        assert state.balance(ADMIN) == OPERATOR_BOND

    def test_settle_before_close_rejected(self, contract, state):
        rid = open_room(contract, commitment=seed_commitment(SEED))
        join_room(contract, state, PLAYER_A, rid, 100)
        with pytest.raises(PluginError, match="not closed"):
            run(contract._deliver_message_settle_room(
                MessageSettleRoom(operator_address=ADMIN, round_id=rid, seed=SEED, pattern="line"),
                99_999))

    def test_settle_before_entropy_available_rejected(self, contract, state):
        rid = open_room(contract, commitment=seed_commitment(SEED))
        join_room(contract, state, PLAYER_A, rid, 100)
        settle_height, _ = close_room(contract, rid)
        with pytest.raises(PluginError, match="entropy not yet available"):
            run(contract._deliver_message_settle_room(
                MessageSettleRoom(operator_address=ADMIN, round_id=rid, seed=SEED, pattern="line"),
                settle_height - 2))

    def test_wrong_seed_rejected(self, contract, state):
        rid = open_room(contract, commitment=seed_commitment(SEED))
        join_room(contract, state, PLAYER_A, rid, 100)
        settle_height, _ = close_room(contract, rid)
        with pytest.raises(PluginError, match="commitment"):
            run(contract._deliver_message_settle_room(
                MessageSettleRoom(operator_address=ADMIN, round_id=rid,
                                  seed=b"wrong seed" + b"\x00" * 22, pattern="line"),
                settle_height))

    def test_non_operator_cannot_settle(self, contract, state):
        rid = open_room(contract, commitment=seed_commitment(SEED))
        join_room(contract, state, PLAYER_A, rid, 100)
        settle_height, _ = close_room(contract, rid)
        impostor = b"x" * 20
        with pytest.raises(PluginError, match="only the operator"):
            run(contract._deliver_message_settle_room(
                MessageSettleRoom(operator_address=impostor, round_id=rid, seed=SEED, pattern="line"),
                settle_height))


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
        join_room(contract, state, PLAYER_B, rid, 180, num_cards=2)

        assert state.balance(escrow_address(rid)) == 280
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
        assert state.balance(PLAYER_B) == 180
        assert state.balance(escrow_address(rid)) == 0
        # an unsettled round slashes the operator bond to the treasury
        assert state.balance(bingo_bond_address(rid)) == 0
        assert state.balance(TREASURY_ADDRESS) == OPERATOR_BOND

    def test_closed_but_unsettled_round_expires_after_grace(self, contract, state):
        rid = open_room(contract, entry_fee=100, height=1000)
        join_room(contract, state, PLAYER_A, rid, 100)
        settle_height, _ = close_room(contract, rid, close_height=1000 + ROOM_EXPIRY_BLOCKS)
        entropy_end = settle_height - 1
        # before the grace period the closed round still cannot be expired
        with pytest.raises(PluginError, match="expiry height"):
            run(contract._deliver_message_expire_room(
                MessageExpireRoom(caller_address=PLAYER_A, round_id=rid),
                height=entropy_end + SETTLE_GRACE_BLOCKS - 1))
        resp = run(contract._deliver_message_expire_room(
            MessageExpireRoom(caller_address=PLAYER_A, round_id=rid),
            height=entropy_end + SETTLE_GRACE_BLOCKS))
        assert not resp.HasField("error"), resp.error.msg
        assert state.balance(PLAYER_A) == 100
        assert state.balance(TREASURY_ADDRESS) == OPERATOR_BOND

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
        settle_height, _ = close_room(contract, rid)
        run(contract._deliver_message_settle_room(
            MessageSettleRoom(operator_address=ADMIN, round_id=rid, seed=SEED, pattern="line"),
            settle_height))

        with pytest.raises(PluginError, match="not open"):
            run(contract._deliver_message_expire_room(
                MessageExpireRoom(caller_address=PLAYER_A, round_id=rid),
                height=settle_height + ROOM_EXPIRY_BLOCKS + SETTLE_GRACE_BLOCKS,
            ))

    def test_unknown_round_rejected(self, contract, state):
        with pytest.raises(PluginError, match="not found"):
            run(contract._deliver_message_expire_room(
                MessageExpireRoom(caller_address=PLAYER_A, round_id=b"nope"),
                height=999_999,
            ))
