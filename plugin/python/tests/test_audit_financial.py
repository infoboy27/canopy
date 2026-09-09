import pytest
from tests.test_room_lifecycle import (
    FakeState, Contract, Config, open_room, close_room, join_room, run, ADMIN,
    PLAYER_A, SEED, seed_commitment, MessageJoinRoom, MessageSettleRoom,
    PluginError, escrow_address, MessageExpireRoom,
    OPERATOR_BOND, fund_operator, entropy_for_close_height, close_round,
)
from tests.test_roulette_lifecycle import open_roulette, close_roulette, DEFAULT_CLOSE_HEIGHT
from tests.test_poker_lifecycle import open_poker, close_poker
from tests.test_domino_lifecycle import open_domino, close_domino
from contract.contract import ADMIN_ADDRESSES, ROOM_EXPIRY_BLOCKS, UINT64_MAX
from contract.contract import TREASURY_ADDRESS, roulette_escrow_address, rng_v2_seed
from contract.game.roulette import spin_number
from contract.proto.tx_pb2 import (
    MessageFaucet, MessageMintCosmetic, MessageOpenRoulette,
    MessageRouletteBet, MessageSettleRoulette,
)
from contract.proto.tx_pb2 import MessageOpenPoker, MessageJoinPoker, MessageSettlePoker, PokerActionRecord
from contract.proto.tx_pb2 import MessageOpenDomino, MessageJoinDomino, MessageSettleDomino, DominoMoveRecord
from contract.game.domino import GameEngine, Move
from contract.contract import domino_escrow_address, poker_escrow_address

@pytest.mark.parametrize('amount', [1,99,101,100000])
def test_wrong_entry_amount_rejected_without_changing_balance(amount):
    state=FakeState(); contract=Contract(config=Config(),plugin=state)
    rid=open_room(contract,entry_fee=100)
    state.set_balance(PLAYER_A,amount)
    before=dict(state.kv)
    with pytest.raises(PluginError,match='entry cost'):
        run(contract._deliver_message_join_room(MessageJoinRoom(player_address=PLAYER_A,round_id=rid,num_cards=1,amount=amount)))
    assert state.kv == before

def test_multicard_floor_matches_server_and_duplicate_join_is_rejected():
    state=FakeState(); contract=Contract(config=Config(),plugin=state)
    rid=open_room(contract,entry_fee=101)
    join_room(contract,state,PLAYER_A,rid,181,num_cards=2)
    assert state.balance(escrow_address(rid)) == 181
    with pytest.raises(PluginError,match='already joined'):
        run(contract._deliver_message_join_room(MessageJoinRoom(player_address=PLAYER_A,round_id=rid,num_cards=2,amount=181)))

def test_operator_cannot_choose_a_different_pattern_at_reveal():
    state=FakeState(); contract=Contract(config=Config(),plugin=state)
    rid=open_room(contract,commitment=seed_commitment(SEED))
    join_room(contract,state,PLAYER_A,rid,100)
    before=dict(state.kv)
    with pytest.raises(PluginError,match='fixed to line'):
        run(contract._deliver_message_settle_room(MessageSettleRoom(operator_address=ADMIN,round_id=rid,seed=SEED,pattern='full_house')))
    assert state.kv == before

def test_only_documented_operator_remains_privileged():
    assert ADMIN_ADDRESSES == frozenset({bytes.fromhex('fb70ee0f20168be6d3a98f13dcbab09b1ea18c65')})

def test_cosmetic_mint_rejects_non_admin_operator():
    contract=Contract(config=Config())
    with pytest.raises(PluginError):
        contract._check_mint_cosmetic(MessageMintCosmetic(
            operator_address=PLAYER_A, owner_address=PLAYER_A,
            token_id=b'audit-token', kind=b'avatar',
        ))

def test_deliver_revalidates_admin_instead_of_trusting_check_tx():
    state=FakeState(); contract=Contract(config=Config(),plugin=state)
    before=dict(state.kv)
    with pytest.raises(PluginError):
        run(contract._deliver_message_faucet(MessageFaucet(
            signer_address=PLAYER_A, recipient_address=PLAYER_A, amount=100,
        )))
    assert state.kv == before

def test_expiry_refund_rejects_recipient_overflow_atomically():
    state=FakeState(); contract=Contract(config=Config(),plugin=state)
    rid=open_room(contract,entry_fee=100,height=1000)
    join_room(contract,state,PLAYER_A,rid,100)
    state.set_balance(PLAYER_A,UINT64_MAX)
    before=dict(state.kv)
    with pytest.raises(PluginError):
        run(contract._deliver_message_expire_room(
            MessageExpireRoom(caller_address=PLAYER_A,round_id=rid),
            height=1000 + ROOM_EXPIRY_BLOCKS,
        ))
    assert state.kv == before

def test_treasury_as_bingo_winner_preserves_rake_and_net():
    state=FakeState(); contract=Contract(config=Config(),plugin=state)
    rid=open_room(contract,commitment=seed_commitment(SEED))
    join_room(contract,state,TREASURY_ADDRESS,rid,100)
    settle_height,_=close_room(contract,rid)
    run(contract._deliver_message_settle_room(MessageSettleRoom(operator_address=ADMIN,round_id=rid,seed=SEED,pattern='line'),settle_height))
    assert state.balance(TREASURY_ADDRESS) == 100
    assert state.balance(escrow_address(rid)) == 0

@pytest.mark.parametrize('bettor', [PLAYER_A,TREASURY_ADDRESS])
def test_roulette_settlement_conserves_funds_with_treasury_alias(bettor):
    state=FakeState(); contract=Contract(config=Config(),plugin=state); rid=b'round001'
    state.set_balance(TREASURY_ADDRESS,100000)
    if bettor != TREASURY_ADDRESS: state.set_balance(bettor,100)
    initial=sum(state.balance(a) for a in {bettor,TREASURY_ADDRESS})
    open_roulette(contract,round_id=rid,commitment=seed_commitment(SEED))
    entropy=entropy_for_close_height(DEFAULT_CLOSE_HEIGHT)
    win_number=spin_number(rng_v2_seed(SEED,entropy,rid))
    run(contract._deliver_message_roulette_bet(MessageRouletteBet(player_address=bettor,round_id=rid,bet_type='straight',bet_number=win_number,amount=100)))
    settle_height,_=close_roulette(contract,rid)
    run(contract._deliver_message_settle_roulette(MessageSettleRoulette(operator_address=ADMIN,round_id=rid,seed=SEED),settle_height))
    assert sum(state.balance(a) for a in {bettor,TREASURY_ADDRESS}) == initial
    assert state.balance(roulette_escrow_address(rid)) == 0
    with pytest.raises(PluginError,match='already settled'):
        run(contract._deliver_message_settle_roulette(MessageSettleRoulette(operator_address=ADMIN,round_id=rid,seed=SEED),settle_height))

@pytest.mark.parametrize('treasury_seat',[0,1])
def test_poker_treasury_participant_preserves_payout_and_rake(treasury_seat):
    state=FakeState(); contract=Contract(config=Config(),plugin=state); rid=b'round001'
    players=[PLAYER_A,PLAYER_A]; players[treasury_seat]=TREASURY_ADDRESS
    open_poker(contract,round_id=rid,commitment=seed_commitment(SEED))
    for player in players:
        state.set_balance(player,1000)
        run(contract._deliver_message_join_poker(MessageJoinPoker(player_address=player,round_id=rid,amount=1000)))
    settle_height,_=close_poker(contract,rid)
    run(contract._deliver_message_settle_poker(MessageSettlePoker(operator_address=ADMIN,round_id=rid,seed=SEED,actions=[PokerActionRecord(action='fold')]),settle_height))
    assert sum(state.balance(a) for a in players) == 2000
    assert state.balance(poker_escrow_address(rid)) == 0

@pytest.mark.parametrize('treasury_seat',[0,1])
def test_domino_treasury_participant_preserves_payout_and_rake(treasury_seat):
    state=FakeState(); contract=Contract(config=Config(),plugin=state); rid=b'round001'
    players=[PLAYER_A,PLAYER_A]; players[treasury_seat]=TREASURY_ADDRESS
    open_domino(contract,round_id=rid,commitment=seed_commitment(SEED))
    for player in players:
        state.set_balance(player,100)
        run(contract._deliver_message_join_domino(MessageJoinDomino(player_address=player,round_id=rid,amount=100)))
    final_seed=rng_v2_seed(SEED,entropy_for_close_height(DEFAULT_CLOSE_HEIGHT),rid)
    engine=GameEngine(final_seed); moves=[]
    while not engine.finished:
        hand=engine.hands[engine.turn]; move=None
        if engine.ends is None: move=Move(action='play',tile=hand[0])
        else:
            for tile in hand:
                if engine.ends[0] in tile: move=Move(action='play',tile=tile,end='left'); break
                if engine.ends[1] in tile: move=Move(action='play',tile=tile,end='right'); break
        if move is None: move=Move(action='draw' if engine.boneyard_idx < len(engine.boneyard) else 'pass')
        engine.apply_move(move)
        moves.append(DominoMoveRecord(action=move.action,tile_low=move.tile[0] if move.tile else 0,tile_high=move.tile[1] if move.tile else 0,end=move.end or ''))
        assert len(moves) < 200
    settle_height,_=close_domino(contract,rid)
    run(contract._deliver_message_settle_domino(MessageSettleDomino(operator_address=ADMIN,round_id=rid,seed=SEED,moves=moves),settle_height))
    assert sum(state.balance(a) for a in players) == 200
    assert state.balance(domino_escrow_address(rid)) == 0
