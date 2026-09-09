"""End-to-end tests of the domino round escrow lifecycle against an
in-memory fake plugin (state_read/state_write backed by a plain dict) -- no
live Canopy node required. Covers open_domino -> join_domino (x2) ->
settle_domino (happy path + rejected illegal move claims + rake) and
open_domino -> join_domino -> expire_domino (abandoned-round refund).

Mirrors test_roulette_lifecycle.py's FakeState harness exactly.
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
    key_for_domino_round,
    domino_escrow_address,
    domino_bond_address,
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
    MessageOpenDomino,
    MessageJoinDomino,
    MessageSettleDomino,
    MessageExpireDomino,
    DominoMoveRecord,
    DominoRound,
)
from contract.game import domino as gdomino
from tests.test_room_lifecycle import (
    OPERATOR_BOND, fund_operator, close_round, entropy_for_close_height,
)

ADMIN = next(iter(ADMIN_ADDRESSES))
PLAYER_A = b"p" * 20
PLAYER_B = b"q" * 20

DEFAULT_RID = b"round001"
DEFAULT_OPEN_HEIGHT = 1000
DEFAULT_CLOSE_HEIGHT = DEFAULT_OPEN_HEIGHT + 1


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


def open_domino(contract, round_id=DEFAULT_RID, entry_fee=100, rake_bps=1000, height=DEFAULT_OPEN_HEIGHT,
                 commitment=b"c" * 32, operator=ADMIN, operator_bond=OPERATOR_BOND):
    if operator == ADMIN:
        fund_operator(contract.plugin, operator_bond)
    msg = MessageOpenDomino(operator_address=operator, round_id=round_id, commitment=commitment,
                             entry_fee=entry_fee, rake_bps=rake_bps, operator_bond=operator_bond)
    resp = run(contract._deliver_message_open_domino(msg, height))
    assert not resp.HasField("error"), resp.error.msg
    return round_id


def close_domino(contract, round_id=DEFAULT_RID, **kw):
    return close_round(contract, round_id, key_for_domino_round(round_id), DominoRound, **kw)


def join_domino(contract, state, player, round_id, amount):
    state.set_balance(player, amount)
    msg = MessageJoinDomino(player_address=player, round_id=round_id, amount=amount)
    resp = run(contract._deliver_message_join_domino(msg))
    assert not resp.HasField("error"), resp.error.msg


def play_out_a_full_game(seed):
    """Drives a real legal game using the simplest available strategy (first
    legal tile found) and returns the resulting move log as DominoMoveRecord
    protos, ready to feed into MessageSettleDomino."""
    hands, boneyard = gdomino.deal(seed)
    ends = None
    turn = 0
    boneyard_idx = 0
    consecutive_passes = 0
    log = []

    while True:
        hand = hands[turn]
        mv = None
        if ends is None:
            mv = gdomino.Move(action="play", tile=hand[0])
        else:
            left, right = ends
            for t in hand:
                a, b = t
                if a == left or b == left:
                    mv = gdomino.Move(action="play", tile=t, end="left")
                    break
                if a == right or b == right:
                    mv = gdomino.Move(action="play", tile=t, end="right")
                    break

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
                    ends = (b if a == left else a, right)
                else:
                    ends = (left, b if a == right else a)
            consecutive_passes = 0
            if not hand:
                break
            turn = (turn + 1) % gdomino.NUM_PLAYERS
        elif boneyard_idx < len(boneyard):
            log.append(gdomino.Move(action="draw"))
            hand.append(boneyard[boneyard_idx])
            boneyard_idx += 1
            consecutive_passes = 0
        else:
            log.append(gdomino.Move(action="pass"))
            consecutive_passes += 1
            if consecutive_passes >= gdomino.NUM_PLAYERS:
                break
            turn = (turn + 1) % gdomino.NUM_PLAYERS

    proto_moves = []
    for m in log:
        low, high = m.tile if m.tile else (0, 0)
        proto_moves.append(DominoMoveRecord(action=m.action, tile_low=low, tile_high=high, end=m.end or ""))
    return proto_moves


def find_seed_with_result(reason, round_id=DEFAULT_RID, close_height=DEFAULT_CLOSE_HEIGHT, start=0):
    """Brute-force a revealed operator secret such that the fair-randomness-v2
    replay seed -- SHA256(secret || consensus_entropy || round_id), with the
    entropy `close_round` fixes for a round closed at `close_height` -- yields a
    game that ends with `reason`. Returns (secret, move_log, result)."""
    entropy = entropy_for_close_height(close_height)
    for i in range(start, start + 20000):
        secret = f"dom-seed-{i}".encode().ljust(32, b"\x00")
        final_seed = rng_v2_seed(secret, entropy, round_id)
        moves = play_out_a_full_game(final_seed)
        py_moves = [gdomino.Move(action=m.action,
                                  tile=(m.tile_low, m.tile_high) if m.action == "play" else None,
                                  end=m.end or None) for m in moves]
        result = gdomino.replay(final_seed, py_moves)
        if result.reason == reason:
            return secret, moves, result
    raise AssertionError(f"no secret found with reason={reason}")


SEED_EMPTY, MOVES_EMPTY, RESULT_EMPTY = find_seed_with_result("emptied_hand")


class TestOpenDomino:
    def test_non_admin_cannot_open(self, contract, state):
        impostor = b"x" * 20
        msg = MessageOpenDomino(operator_address=impostor, round_id=b"round001",
                                 commitment=b"c" * 32, entry_fee=100, rake_bps=1000)
        with pytest.raises(PluginError):
            contract._check_message_open_domino(msg)


class TestJoinDomino:
    def test_third_join_rejected(self, contract, state):
        rid = open_domino(contract, entry_fee=100)
        join_domino(contract, state, PLAYER_A, rid, 100)
        join_domino(contract, state, PLAYER_B, rid, 100)
        third = b"z" * 20
        state.set_balance(third, 100)
        with pytest.raises(PluginError, match="enough players"):
            run(contract._deliver_message_join_domino(
                MessageJoinDomino(player_address=third, round_id=rid, amount=100)))

    def test_same_address_cannot_join_twice(self, contract, state):
        rid = open_domino(contract, entry_fee=100)
        join_domino(contract, state, PLAYER_A, rid, 100)
        state.set_balance(PLAYER_A, 100)
        with pytest.raises(PluginError, match="already joined"):
            run(contract._deliver_message_join_domino(
                MessageJoinDomino(player_address=PLAYER_A, round_id=rid, amount=100)))

    def test_wrong_amount_rejected(self, contract, state):
        rid = open_domino(contract, entry_fee=100)
        state.set_balance(PLAYER_A, 999)
        with pytest.raises(PluginError, match="entry fee"):
            run(contract._deliver_message_join_domino(
                MessageJoinDomino(player_address=PLAYER_A, round_id=rid, amount=999)))


class TestSettleDominoHappyPath:
    def test_winner_takes_the_pot_net_of_rake(self, contract, state):
        rid = open_domino(contract, entry_fee=100, rake_bps=1000, commitment=seed_commitment(SEED_EMPTY))
        join_domino(contract, state, PLAYER_A, rid, 100)
        join_domino(contract, state, PLAYER_B, rid, 100)
        settle_height, _ = close_domino(contract, rid)

        resp = run(contract._deliver_message_settle_domino(
            MessageSettleDomino(operator_address=ADMIN, round_id=rid, seed=SEED_EMPTY, moves=MOVES_EMPTY),
            settle_height))
        assert not resp.HasField("error"), resp.error.msg

        winner_addr = [PLAYER_A, PLAYER_B][RESULT_EMPTY.winners[0]]
        loser_addr = [PLAYER_A, PLAYER_B][1 - RESULT_EMPTY.winners[0]]
        # pot=200, 10% rake=20, net=180 to the sole winner
        assert state.balance(winner_addr) == 180
        assert state.balance(loser_addr) == 0
        assert state.balance(TREASURY_ADDRESS) == 20
        assert state.balance(domino_escrow_address(rid)) == 0

    def test_wrong_seed_rejected(self, contract, state):
        rid = open_domino(contract, commitment=seed_commitment(SEED_EMPTY))
        join_domino(contract, state, PLAYER_A, rid, 100)
        join_domino(contract, state, PLAYER_B, rid, 100)
        with pytest.raises(PluginError, match="commitment"):
            run(contract._deliver_message_settle_domino(
                MessageSettleDomino(operator_address=ADMIN, round_id=rid,
                                     seed=b"wrong seed" + b"\x00" * 22, moves=MOVES_EMPTY)))

    def test_non_operator_cannot_settle(self, contract, state):
        rid = open_domino(contract, commitment=seed_commitment(SEED_EMPTY))
        join_domino(contract, state, PLAYER_A, rid, 100)
        join_domino(contract, state, PLAYER_B, rid, 100)
        impostor = b"x" * 20
        with pytest.raises(PluginError, match="only the operator"):
            run(contract._deliver_message_settle_domino(
                MessageSettleDomino(operator_address=impostor, round_id=rid, seed=SEED_EMPTY, moves=MOVES_EMPTY)))

    def test_settle_before_both_seats_filled_rejected(self, contract, state):
        rid = open_domino(contract, commitment=seed_commitment(SEED_EMPTY))
        join_domino(contract, state, PLAYER_A, rid, 100)
        with pytest.raises(PluginError, match="never filled both seats"):
            run(contract._deliver_message_settle_domino(
                MessageSettleDomino(operator_address=ADMIN, round_id=rid, seed=SEED_EMPTY, moves=MOVES_EMPTY)))

    def test_claimed_illegal_move_rejected(self, contract, state):
        rid = open_domino(contract, commitment=seed_commitment(SEED_EMPTY))
        join_domino(contract, state, PLAYER_A, rid, 100)
        join_domino(contract, state, PLAYER_B, rid, 100)
        bogus_moves = [DominoMoveRecord(action="play", tile_low=9, tile_high=9)]  # not a real tile
        settle_height, _ = close_domino(contract, rid)
        with pytest.raises(PluginError, match="illegal move"):
            run(contract._deliver_message_settle_domino(
                MessageSettleDomino(operator_address=ADMIN, round_id=rid, seed=SEED_EMPTY, moves=bogus_moves),
                settle_height))

    def test_cannot_settle_twice(self, contract, state):
        rid = open_domino(contract, commitment=seed_commitment(SEED_EMPTY))
        join_domino(contract, state, PLAYER_A, rid, 100)
        join_domino(contract, state, PLAYER_B, rid, 100)
        settle_height, _ = close_domino(contract, rid)
        run(contract._deliver_message_settle_domino(
            MessageSettleDomino(operator_address=ADMIN, round_id=rid, seed=SEED_EMPTY, moves=MOVES_EMPTY),
            settle_height))
        with pytest.raises(PluginError, match="already settled"):
            run(contract._deliver_message_settle_domino(
                MessageSettleDomino(operator_address=ADMIN, round_id=rid, seed=SEED_EMPTY, moves=MOVES_EMPTY),
                settle_height))


class TestExpireDomino:
    def test_refund_before_deadline_rejected(self, contract, state):
        rid = open_domino(contract, entry_fee=100, height=1000)
        join_domino(contract, state, PLAYER_A, rid, 100)

        with pytest.raises(PluginError) as exc:
            run(contract._deliver_message_expire_domino(
                MessageExpireDomino(caller_address=PLAYER_A, round_id=rid),
                height=1000 + ROOM_EXPIRY_BLOCKS - 1,
            ))
        assert exc.value.code == 16  # err_room_not_expired

    def test_refunds_every_participant_after_deadline(self, contract, state):
        rid = open_domino(contract, entry_fee=100, height=1000)
        join_domino(contract, state, PLAYER_A, rid, 100)
        join_domino(contract, state, PLAYER_B, rid, 100)

        assert state.balance(domino_escrow_address(rid)) == 200
        caller = b"z" * 20  # anyone may call this
        resp = run(contract._deliver_message_expire_domino(
            MessageExpireDomino(caller_address=caller, round_id=rid),
            height=1000 + ROOM_EXPIRY_BLOCKS,
        ))
        assert not resp.HasField("error"), resp.error.msg

        assert state.balance(PLAYER_A) == 100
        assert state.balance(PLAYER_B) == 100
        assert state.balance(domino_escrow_address(rid)) == 0

    def test_refunds_a_lone_participant_if_second_seat_never_filled(self, contract, state):
        rid = open_domino(contract, entry_fee=100, height=1000)
        join_domino(contract, state, PLAYER_A, rid, 100)

        resp = run(contract._deliver_message_expire_domino(
            MessageExpireDomino(caller_address=PLAYER_A, round_id=rid),
            height=1000 + ROOM_EXPIRY_BLOCKS,
        ))
        assert not resp.HasField("error"), resp.error.msg
        assert state.balance(PLAYER_A) == 100

    def test_cannot_expire_twice(self, contract, state):
        rid = open_domino(contract, entry_fee=100, height=1000)
        join_domino(contract, state, PLAYER_A, rid, 100)
        expire_height = 1000 + ROOM_EXPIRY_BLOCKS
        run(contract._deliver_message_expire_domino(
            MessageExpireDomino(caller_address=PLAYER_A, round_id=rid), height=expire_height))

        with pytest.raises(PluginError, match="not open"):
            run(contract._deliver_message_expire_domino(
                MessageExpireDomino(caller_address=PLAYER_A, round_id=rid), height=expire_height + 1))

    def test_settled_round_cannot_be_expired(self, contract, state):
        rid = open_domino(contract, entry_fee=100, rake_bps=0, height=1000, commitment=seed_commitment(SEED_EMPTY))
        join_domino(contract, state, PLAYER_A, rid, 100)
        join_domino(contract, state, PLAYER_B, rid, 100)
        settle_height, _ = close_domino(contract, rid)
        run(contract._deliver_message_settle_domino(
            MessageSettleDomino(operator_address=ADMIN, round_id=rid, seed=SEED_EMPTY, moves=MOVES_EMPTY),
            settle_height))

        with pytest.raises(PluginError, match="not open"):
            run(contract._deliver_message_expire_domino(
                MessageExpireDomino(caller_address=PLAYER_A, round_id=rid),
                height=settle_height + ROOM_EXPIRY_BLOCKS + SETTLE_GRACE_BLOCKS,
            ))

    def test_unknown_round_rejected(self, contract, state):
        with pytest.raises(PluginError, match="not found"):
            run(contract._deliver_message_expire_domino(
                MessageExpireDomino(caller_address=PLAYER_A, round_id=b"nope"),
                height=999_999,
            ))
