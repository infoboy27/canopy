"""Fair-randomness-v2 (audit/specs/fair-randomness-v2.md, Option A) end-to-end.

The operator commits a secret at open, betting is stopped by an explicit close
BEFORE any block in the entropy window exists, the plugin folds a window of
finalized consensus block hashes from its own FSM-fed ledger at settle, and the
replay seed is SHA256(secret || entropy || round_id). These tests drive the
whole open -> join -> close -> (advance blocks) -> settle path against the
in-memory fake plugin and independently recompute the seed and the outcome.
"""

import pytest

from contract.contract import (
    Contract, ADMIN_ADDRESSES, TREASURY_ADDRESS,
    ENTROPY_DELAY_BLOCKS, ENTROPY_WINDOW_BLOCKS, SETTLE_GRACE_BLOCKS,
    seed_commitment, key_for_account, key_for_round, key_for_consensus_entropy,
    escrow_address, bingo_bond_address,
    encode_consensus_entropy, fold_consensus_entropy, rng_v2_seed,
    marshal, unmarshal,
)
from contract.plugin import Config
from contract.error import PluginError
from contract.proto import Account, PluginBeginRequest
from contract.proto.tx_pb2 import (
    MessageOpenRoom, MessageCloseRoom, MessageJoinRoom, MessageSettleRoom,
    MessageExpireRoom, RoomRound,
)
from contract.game import draw as gdraw, card as gcard, rules as grules
from contract.game.rng import derive_seed
from tests.test_room_lifecycle import FakeState, run, ADMIN

PLAYER_A = b"a" * 20
PLAYER_B = b"b" * 20
SECRET = b"operator-secret-v2".ljust(32, b"\x00")
RID = b"fair-v2-round"


@pytest.fixture
def state():
    return FakeState()


@pytest.fixture
def contract(state):
    state.set_balance(ADMIN, 10_000)
    return Contract(config=Config(), plugin=state)


def _begin_block(contract, height, block_hash):
    """Drive the real begin_block so the entropy ledger is populated exactly the
    way a running chain would, rather than writing ledger keys by hand."""
    resp = run(contract.begin_block(
        PluginBeginRequest(height=height, last_block_hash=block_hash)))
    assert not resp.HasField("error"), resp.error.msg


def _run_blocks(contract, lo, hi):
    """begin_block for every height in [lo, hi]; returns {predecessor_height: hash}."""
    hashes = {}
    for h in range(lo, hi + 1):
        bh = derive_seed(b"block", h.to_bytes(8, "big"))
        _begin_block(contract, h, bh)
        hashes[h - 1] = bh
    return hashes


def _open(contract, state, *, entry_fee=100, rake_bps=1000, bond=500, height=1000):
    resp = run(contract._deliver_message_open_room(
        MessageOpenRoom(operator_address=ADMIN, round_id=RID,
                        commitment=seed_commitment(SECRET), entry_fee=entry_fee,
                        rake_bps=rake_bps, payout_weights_bps=[10000],
                        operator_bond=bond), height))
    assert not resp.HasField("error"), resp.error.msg


def _join(contract, state, player, amount, num_cards=1):
    state.set_balance(player, amount)
    resp = run(contract._deliver_message_join_room(
        MessageJoinRoom(player_address=player, round_id=RID,
                        num_cards=num_cards, amount=amount)))
    assert not resp.HasField("error"), resp.error.msg


def _close(contract, height):
    resp = run(contract._deliver_close_round(
        key_for_round(RID), RoomRound,
        MessageCloseRoom(operator_address=ADMIN, round_id=RID), height))
    assert not resp.HasField("error"), resp.error.msg
    rr = RoomRound(); rr.ParseFromString(contract.plugin.kv[key_for_round(RID)])
    return rr


def test_close_fixes_a_future_entropy_window():
    """At close, none of the window blocks exist yet -- the outcome is not
    determined by anyone."""
    state = FakeState(); state.set_balance(ADMIN, 10_000)
    contract = Contract(config=Config(), plugin=state)
    _open(contract, state, height=1000)
    _join(contract, state, PLAYER_A, 100)
    rr = _close(contract, height=1000)
    assert rr.status == 3
    assert rr.close_height == 1000
    assert rr.entropy_start == 1000 + ENTROPY_DELAY_BLOCKS
    assert rr.entropy_end == rr.entropy_start + ENTROPY_WINDOW_BLOCKS - 1
    # the ledger has nothing at or past the window yet
    for h in range(rr.entropy_start, rr.entropy_end + 1):
        assert key_for_consensus_entropy(h) not in state.kv


def test_settle_replays_from_folded_consensus_entropy(contract, state):
    _open(contract, state, entry_fee=100, rake_bps=1000, bond=500, height=1000)
    _join(contract, state, PLAYER_A, 100)
    rr = _close(contract, height=1000)

    # advance the chain through and past the entropy window
    hashes = _run_blocks(contract, rr.close_height + 1, rr.entropy_end + 1)

    settle_height = rr.entropy_end + 1
    resp = run(contract._deliver_message_settle_room(
        MessageSettleRoom(operator_address=ADMIN, round_id=RID, seed=SECRET,
                          pattern="line"), settle_height))
    assert not resp.HasField("error"), resp.error.msg

    # independent recomputation of the seed the plugin must have used
    window = [encode_consensus_entropy(hashes[h], b"")
              for h in range(rr.entropy_start, rr.entropy_end + 1)]
    entropy = fold_consensus_entropy(window)
    final_seed = rng_v2_seed(SECRET, entropy, RID)

    order = gdraw.draw_order(final_seed)
    cards = gcard.generate_cards(derive_seed(final_seed, PLAYER_A), 1)
    idxs = [grules.first_win_index(c, order, grules.Pattern("line")) for c in cards]
    assert any(x > 0 for x in idxs)  # the sole player wins once every ball is drawn

    # 100 escrowed, 10% rake -> 10 treasury, 90 to the winner; bond returned
    assert state.balance(TREASURY_ADDRESS) == 10
    assert state.balance(PLAYER_A) == 90
    assert state.balance(escrow_address(RID)) == 0
    assert state.balance(bingo_bond_address(RID)) == 0
    assert state.balance(ADMIN) == 10_000  # bond escrowed then returned in full

    rr2 = RoomRound(); rr2.ParseFromString(state.kv[key_for_round(RID)])
    assert rr2.status == 1


def test_a_different_window_yields_a_different_seed(contract, state):
    """Grinding one block does not help: a single changed hash in the window
    changes the folded entropy, hence the replay seed."""
    _open(contract, state, height=1000)
    _join(contract, state, PLAYER_A, 100)
    rr = _close(contract, height=1000)
    hashes = _run_blocks(contract, rr.close_height + 1, rr.entropy_end + 1)

    base = [encode_consensus_entropy(hashes[h], b"")
            for h in range(rr.entropy_start, rr.entropy_end + 1)]
    grinded = list(base)
    grinded[-1] = encode_consensus_entropy(b"\xff" * 32, b"")
    assert fold_consensus_entropy(base) != fold_consensus_entropy(grinded)
    assert (rng_v2_seed(SECRET, fold_consensus_entropy(base), RID)
            != rng_v2_seed(SECRET, fold_consensus_entropy(grinded), RID))


def test_settle_fails_closed_when_a_window_block_is_missing(contract, state):
    _open(contract, state, height=1000)
    _join(contract, state, PLAYER_A, 100)
    rr = _close(contract, height=1000)
    # advance far enough in height, but drop one block inside the window
    for h in range(rr.close_height + 1, rr.entropy_end + 2):
        if h - 1 == rr.entropy_start + 3:
            continue  # this predecessor hash never lands in the ledger
        _begin_block(contract, h, derive_seed(b"block", h.to_bytes(8, "big")))
    with pytest.raises(PluginError, match="not available"):
        run(contract._deliver_message_settle_room(
            MessageSettleRoom(operator_address=ADMIN, round_id=RID, seed=SECRET,
                              pattern="line"), rr.entropy_end + 1))


def test_settle_rejected_before_the_window_finalizes(contract, state):
    _open(contract, state, height=1000)
    _join(contract, state, PLAYER_A, 100)
    rr = _close(contract, height=1000)
    _run_blocks(contract, rr.close_height + 1, rr.entropy_end)  # one short
    with pytest.raises(PluginError, match="entropy not yet available"):
        run(contract._deliver_message_settle_room(
            MessageSettleRoom(operator_address=ADMIN, round_id=RID, seed=SECRET,
                              pattern="line"), rr.entropy_end))


def test_join_rejected_after_close(contract, state):
    _open(contract, state, height=1000)
    _join(contract, state, PLAYER_A, 100)
    _close(contract, height=1000)
    state.set_balance(PLAYER_B, 100)
    with pytest.raises(PluginError, match="not open"):
        run(contract._deliver_message_join_room(
            MessageJoinRoom(player_address=PLAYER_B, round_id=RID,
                            num_cards=1, amount=100)))


def test_non_admin_cannot_close(contract, state):
    _open(contract, state, height=1000)
    _join(contract, state, PLAYER_A, 100)
    with pytest.raises(PluginError, match="not authorized"):
        run(contract._deliver_close_round(
            key_for_round(RID), RoomRound,
            MessageCloseRoom(operator_address=b"z" * 20, round_id=RID), 1001))


def test_close_rejected_when_operator_mismatches_round(contract, state):
    """The operator field is authenticated against the round record, not just
    the admin set -- exercised directly since the harness has one admin key."""
    _open(contract, state, height=1000)
    rr = RoomRound(); rr.ParseFromString(state.kv[key_for_round(RID)])
    rr.operator_address = b"other-operator------"[:20]
    state.kv[key_for_round(RID)] = rr.SerializeToString()
    with pytest.raises(PluginError, match="only the operator can close"):
        run(contract._deliver_close_round(
            key_for_round(RID), RoomRound,
            MessageCloseRoom(operator_address=ADMIN, round_id=RID), 1001))


def test_check_close_requires_an_admin(contract):
    with pytest.raises(PluginError):
        contract._check_message_close(
            MessageCloseRoom(operator_address=b"z" * 20, round_id=RID))


def test_operator_cannot_join_its_own_round(contract, state):
    _open(contract, state, height=1000)
    state.set_balance(ADMIN, state.balance(ADMIN) + 100)
    with pytest.raises(PluginError, match="operator cannot play"):
        run(contract._deliver_message_join_room(
            MessageJoinRoom(player_address=ADMIN, round_id=RID,
                            num_cards=1, amount=100)))


def test_unsettled_closed_round_slashes_bond_on_expire(contract, state):
    _open(contract, state, entry_fee=100, bond=500, height=1000)
    _join(contract, state, PLAYER_A, 100)
    rr = _close(contract, height=1000)
    deadline = rr.entropy_end + SETTLE_GRACE_BLOCKS
    resp = run(contract._deliver_message_expire_room(
        MessageExpireRoom(caller_address=PLAYER_A, round_id=RID), deadline))
    assert not resp.HasField("error"), resp.error.msg
    assert state.balance(PLAYER_A) == 100          # stake refunded
    assert state.balance(bingo_bond_address(RID)) == 0
    assert state.balance(TREASURY_ADDRESS) == 500  # bond slashed to the house
    assert state.balance(ADMIN) == 9_500           # operator is out the bond
