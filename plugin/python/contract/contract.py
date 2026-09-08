"""
Contract implementation for Canopy blockchain plugin.

This file contains the base contract implementation that handles the 'send' transaction.
Matches Go's contract/contract.go structure.
"""

import random
import struct
import hashlib
from typing import Optional, Dict, Any, Union, Protocol, TYPE_CHECKING

UINT64_MAX = (1 << 64) - 1

if TYPE_CHECKING:
    from .plugin import Plugin, Config

# Import proto types
from .proto import (
    PluginCheckRequest,
    PluginCheckResponse,
    PluginDeliverRequest,
    PluginDeliverResponse,
    PluginGenesisRequest,
    PluginGenesisResponse,
    PluginBeginRequest,
    PluginBeginResponse,
    PluginEndRequest,
    PluginEndResponse,
    MessageSend,
    MessageFaucet,
    MessageReward,
    Faucet,
    Reward,
    PluginKeyRead,
    PluginStateReadRequest,
    PluginStateWriteRequest,
    PluginSetOp,
    PluginDeleteOp,
    PluginFSMConfig,
    FeeParams,
    Account,
    Pool,
)
from .proto import account_pb2, event_pb2, plugin_pb2, tx_pb2
from google.protobuf import any_pb2
from .proto.tx_pb2 import (
    MessageOpenRoom,
    MessageJoinRoom,
    MessageSettleRoom,
    MessageExpireRoom,
    RoomRound,
    RoomParticipant,
    MessageBuyCoins,
    MessageBuyGems,
    MessageTransferGems,
    MessageMintCosmetic,
    MessageBuyCosmetic,
    MessageTransferCosmetic,
    GemBalance,
    Cosmetic,
    MessageOpenRoulette,
    MessageRouletteBet,
    MessageSettleRoulette,
    MessageExpireRoulette,
    RouletteRound,
    RouletteBetRecord,
    MessageOpenDomino,
    MessageJoinDomino,
    MessageSettleDomino,
    MessageExpireDomino,
    DominoRound,
)
from .game import card as gcard, draw as gdraw, rules as grules, economy as gecon
from .game import roulette as groulette
from .game import domino as gdomino
from .game.rng import derive_seed, commitment as seed_commitment

from .error import (
    PluginError,
    err_invalid_address,
    err_invalid_amount,
    err_insufficient_funds,
    err_tx_fee_below_state_limit,
    err_invalid_message_cast,
    err_unauthorized_signer,
    err_room_not_expired,
    err_unmarshal,
)

# Blocks a room may stay open before anyone can call MessageExpireRoom to
# refund it. Tune to the chain's actual block time (this default assumes
# ~5s blocks, i.e. ~1 hour) -- long enough that a slow-but-honest operator
# isn't punished, short enough that abandoned rooms don't lock funds for long.
ROOM_EXPIRY_BLOCKS = 720


# Plugin configuration (matching Go's ContractConfig)
CONTRACT_CONFIG = {
    "name": "python_plugin_contract",
    "id": 1,
    "version": 1,
    "supported_transactions": ["send", "faucet", "reward", "open_room", "join_room", "settle_room", "expire_room", "buy_coins", "buy_gems", "transfer_gems", "mint_cosmetic", "buy_cosmetic", "transfer_cosmetic", "open_roulette", "roulette_bet", "settle_roulette", "expire_roulette", "open_domino", "join_domino", "settle_domino", "expire_domino"],
    "transaction_type_urls": [
        "type.googleapis.com/types.MessageSend",
        "type.googleapis.com/types.MessageFaucet",
        "type.googleapis.com/types.MessageReward",
        "type.googleapis.com/types.MessageOpenRoom",
        "type.googleapis.com/types.MessageJoinRoom",
        "type.googleapis.com/types.MessageSettleRoom",
        "type.googleapis.com/types.MessageExpireRoom",
        "type.googleapis.com/types.MessageBuyCoins",
        "type.googleapis.com/types.MessageBuyGems",
        "type.googleapis.com/types.MessageTransferGems",
        "type.googleapis.com/types.MessageMintCosmetic",
        "type.googleapis.com/types.MessageBuyCosmetic",
        "type.googleapis.com/types.MessageTransferCosmetic",
        "type.googleapis.com/types.MessageOpenRoulette",
        "type.googleapis.com/types.MessageRouletteBet",
        "type.googleapis.com/types.MessageSettleRoulette",
        "type.googleapis.com/types.MessageExpireRoulette",
        "type.googleapis.com/types.MessageOpenDomino",
        "type.googleapis.com/types.MessageJoinDomino",
        "type.googleapis.com/types.MessageSettleDomino",
        "type.googleapis.com/types.MessageExpireDomino",
    ],
    "event_type_urls": [],
    "custom_state_prefixes": [b"\x64", b"\x65", b"\x6e", b"\x6f", b"\x70", b"\x71", b"\x72", b"\x73", b"\x74"],  # +112 gems,113 cosmetic,114/115 roulette,116 domino
    # Include google/protobuf/any.proto first as it's a dependency of event.proto and tx.proto
    "file_descriptor_protos": [
        any_pb2.DESCRIPTOR.serialized_pb,
        account_pb2.DESCRIPTOR.serialized_pb,
        event_pb2.DESCRIPTOR.serialized_pb,
        plugin_pb2.DESCRIPTOR.serialized_pb,
        tx_pb2.DESCRIPTOR.serialized_pb,
    ],
}


# State key prefixes (matching Go)
ACCOUNT_PREFIX = b"\x01"
POOL_PREFIX = b"\x02"
PARAMS_PREFIX = b"\x07"


# Key generation functions (from keys.py)

def join_len_prefix(*items: Optional[bytes]) -> bytes:
    """Join byte arrays with length prefixes."""
    result = bytearray()
    for item in items:
        if not item:
            continue
        if len(item) > 255:
            raise ValueError(f"Item too long: {len(item)} bytes (max 255)")
        result.append(len(item))
        result.extend(item)
    return bytes(result)


def format_uint64(value: Union[int, str]) -> bytes:
    """Format uint64 as big-endian bytes."""
    if isinstance(value, str):
        value = int(value)
    if not isinstance(value, int) or value < 0 or value >= (1 << 64):
        raise ValueError(f"Invalid uint64 value: {value}")
    return struct.pack('>Q', value)


def key_for_account(address: bytes) -> bytes:
    """Generate state database key for an account."""
    return join_len_prefix(ACCOUNT_PREFIX, address)


def key_for_fee_params() -> bytes:
    """Generate state database key for fee parameters."""
    return join_len_prefix(PARAMS_PREFIX, b"/f/")


def key_for_fee_pool(chain_id: int) -> bytes:
    """Generate state database key for fee pool."""
    return join_len_prefix(POOL_PREFIX, format_uint64(chain_id))


FAUCET_PREFIX = b"\x64"  # 100 — plugin-owned record, outside Canopy reserved 1-15
REWARD_PREFIX = b"\x65"  # 101


def key_for_faucet(address: bytes) -> bytes:
    """State key for a per-recipient faucet record."""
    return join_len_prefix(FAUCET_PREFIX, address)


TREASURY_ADDRESS = bytes.fromhex("4919006f0f09b382befcc08611052f6d62a5d36e")  # house treasury: rake destination (owner-controlled)
# Rotated 2026-09-07: the prior treasury key (a565c2cc9f4a18a62a2c6a288428850f276c8d0e)
# had been pasted in plaintext across chat sessions and is treated as compromised.
# It still holds a small pre-rotation balance to be swept manually.

# Addresses allowed to mint (faucet/reward/buy_coins/buy_gems). Without this,
# any signed tx naming itself as signer/admin could mint unlimited coins or
# gems to any recipient — none of these messages carry any other proof of
# privilege. Single-operator model for now, same as room settlement; move to
# on-chain governance (multisig/DAO) before this chain holds real value.
ADMIN_ADDRESSES = frozenset({
    bytes.fromhex("fb70ee0f20168be6d3a98f13dcbab09b1ea18c65"),  # casino-gameserver operator key
})


def key_for_reward(address: bytes) -> bytes:
    """State key for a per-recipient reward record."""
    return join_len_prefix(REWARD_PREFIX, address)


# Proto marshal/unmarshal utilities

def marshal(message: Any) -> bytes:
    """Marshal object to protobuf bytes."""
    try:
        if hasattr(message, 'SerializeToString'):
            return message.SerializeToString()
        raise ValueError("Message does not support serialization")
    except Exception as err:
        raise err_unmarshal(err)


def unmarshal(message_type: Any, data: Optional[bytes]) -> Optional[Any]:
    """Unmarshal bytes to protobuf message."""
    if not data:
        return None
    try:
        if hasattr(message_type, 'FromString'):
            return message_type.FromString(data)
        raise ValueError("Message type does not support deserialization")
    except Exception as err:
        raise err_unmarshal(err)


ROUND_PREFIX = b"\x6e"        # 110
PARTICIPANT_PREFIX = b"\x6f"  # 111


def key_for_round(round_id: bytes) -> bytes:
    """State key for a room round record."""
    return join_len_prefix(ROUND_PREFIX, round_id)


def key_for_participant(round_id: bytes, address: bytes) -> bytes:
    """State key for one participant in a round."""
    return join_len_prefix(PARTICIPANT_PREFIX, round_id, address)


def escrow_address(round_id: bytes) -> bytes:
    """Deterministic 20-byte account address holding a round escrow."""
    return hashlib.sha256(b"bingo-escrow" + bytes(round_id)).digest()[:20]


GEM_PREFIX = b"\x70"       # 112
COSMETIC_PREFIX = b"\x71"  # 113


def key_for_gems(address: bytes) -> bytes:
    """State key for a per-address gem balance."""
    return join_len_prefix(GEM_PREFIX, address)


def key_for_cosmetic(token_id: bytes) -> bytes:
    """State key for a cosmetic NFT record."""
    return join_len_prefix(COSMETIC_PREFIX, token_id)


ROULETTE_ROUND_PREFIX = b"\x72"  # 114
ROULETTE_BET_PREFIX = b"\x73"    # 115


def key_for_roulette_round(round_id: bytes) -> bytes:
    """State key for a roulette round record."""
    return join_len_prefix(ROULETTE_ROUND_PREFIX, round_id)


def key_for_roulette_bet(round_id: bytes, address: bytes) -> bytes:
    """State key for one address's bet in a roulette round."""
    return join_len_prefix(ROULETTE_BET_PREFIX, round_id, address)


def roulette_escrow_address(round_id: bytes) -> bytes:
    """Deterministic 20-byte account address holding a roulette round's escrow.
    Distinct salt from Bingo's escrow_address() so the two games never collide
    on the same round_id."""
    return hashlib.sha256(b"roulette-escrow" + bytes(round_id)).digest()[:20]


DOMINO_ROUND_PREFIX = b"\x74"  # 116


def key_for_domino_round(round_id: bytes) -> bytes:
    """State key for a domino round record. No separate per-participant
    record like Bingo's -- capped at exactly 2 players, both addresses live
    directly on DominoRound.participant_addresses."""
    return join_len_prefix(DOMINO_ROUND_PREFIX, round_id)


def domino_escrow_address(round_id: bytes) -> bytes:
    """Deterministic 20-byte account address holding a domino round's escrow.
    Distinct salt so it never collides with Bingo's or Roulette's escrow for
    the same round_id."""
    return hashlib.sha256(b"domino-escrow" + bytes(round_id)).digest()[:20]


class Contract:
    """
    Contract defines the smart contract that implements the extended logic of the nested chain.
    Matches Go's Contract struct.
    """

    def __init__(
        self,
        config: Optional["Config"] = None,
        fsm_config: Optional[PluginFSMConfig] = None,
        plugin: Optional["Plugin"] = None,
        fsm_id: Optional[int] = None,
    ):
        self.config = config
        self.fsm_config = fsm_config
        self.plugin = plugin
        self.fsm_id = fsm_id

    def genesis(self, request: PluginGenesisRequest) -> PluginGenesisResponse:
        """Genesis implements logic to import a json file to create the state at height 0."""
        return PluginGenesisResponse()

    def begin_block(self, request: PluginBeginRequest) -> PluginBeginResponse:
        """BeginBlock is code that is executed at the start of applying the block."""
        return PluginBeginResponse()

    async def check_tx(self, request: PluginCheckRequest) -> PluginCheckResponse:
        """CheckTx is code that is executed to statelessly validate a transaction."""
        try:
            if not self.plugin or not self.config:
                raise PluginError(1, "plugin", "plugin or config not initialized")

            # Validate fee - read fee params from state
            resp = await self.plugin.state_read(
                self,
                PluginStateReadRequest(
                    keys=[PluginKeyRead(query_id=random.randint(0, 2**53), key=key_for_fee_params())]
                ),
            )

            if resp.HasField("error"):
                response = PluginCheckResponse()
                response.error.CopyFrom(resp.error)
                return response

            # Convert bytes into fee parameters
            if not resp.results or not resp.results[0].entries:
                raise PluginError(1, "plugin", "Fee parameters not found")

            fee_params_bytes = resp.results[0].entries[0].value
            min_fees = unmarshal(FeeParams, fee_params_bytes)
            if not min_fees:
                raise PluginError(1, "plugin", "Failed to decode fee parameters")

            # Check for minimum fee
            if request.tx.fee < min_fees.send_fee:
                raise err_tx_fee_below_state_limit()

            # Get the message and handle by type
            type_url = request.tx.msg.type_url
            if type_url.endswith("/types.MessageSend"):
                msg = MessageSend()
                msg.ParseFromString(request.tx.msg.value)
                return self._check_message_send(msg)
            elif type_url.endswith("/types.MessageFaucet"):
                msg = MessageFaucet()
                msg.ParseFromString(request.tx.msg.value)
                return self._check_message_faucet(msg)
            elif type_url.endswith("/types.MessageReward"):
                msg = MessageReward()
                msg.ParseFromString(request.tx.msg.value)
                return self._check_message_reward(msg)
            elif type_url.endswith("/types.MessageOpenRoom"):
                msg = MessageOpenRoom()
                msg.ParseFromString(request.tx.msg.value)
                return self._check_message_open_room(msg)
            elif type_url.endswith("/types.MessageJoinRoom"):
                msg = MessageJoinRoom()
                msg.ParseFromString(request.tx.msg.value)
                return self._check_message_join_room(msg)
            elif type_url.endswith("/types.MessageSettleRoom"):
                msg = MessageSettleRoom()
                msg.ParseFromString(request.tx.msg.value)
                return self._check_message_settle_room(msg)
            elif type_url.endswith("/types.MessageExpireRoom"):
                msg = MessageExpireRoom()
                msg.ParseFromString(request.tx.msg.value)
                return self._check_message_expire_room(msg)
            elif type_url.endswith("/types.MessageBuyCoins"):
                msg = MessageBuyCoins(); msg.ParseFromString(request.tx.msg.value)
                return self._check_mint_like(msg.admin_address, msg.recipient_address, msg.amount)
            elif type_url.endswith("/types.MessageBuyGems"):
                msg = MessageBuyGems(); msg.ParseFromString(request.tx.msg.value)
                return self._check_mint_like(msg.admin_address, msg.recipient_address, msg.amount)
            elif type_url.endswith("/types.MessageTransferGems"):
                msg = MessageTransferGems(); msg.ParseFromString(request.tx.msg.value)
                return self._check_transfer_gems(msg)
            elif type_url.endswith("/types.MessageMintCosmetic"):
                msg = MessageMintCosmetic(); msg.ParseFromString(request.tx.msg.value)
                return self._check_mint_cosmetic(msg)
            elif type_url.endswith("/types.MessageBuyCosmetic"):
                msg = MessageBuyCosmetic(); msg.ParseFromString(request.tx.msg.value)
                return self._check_buy_cosmetic(msg)
            elif type_url.endswith("/types.MessageTransferCosmetic"):
                msg = MessageTransferCosmetic(); msg.ParseFromString(request.tx.msg.value)
                return self._check_transfer_cosmetic(msg)
            elif type_url.endswith("/types.MessageOpenRoulette"):
                msg = MessageOpenRoulette(); msg.ParseFromString(request.tx.msg.value)
                return self._check_message_open_roulette(msg)
            elif type_url.endswith("/types.MessageRouletteBet"):
                msg = MessageRouletteBet(); msg.ParseFromString(request.tx.msg.value)
                return self._check_message_roulette_bet(msg)
            elif type_url.endswith("/types.MessageSettleRoulette"):
                msg = MessageSettleRoulette(); msg.ParseFromString(request.tx.msg.value)
                return self._check_message_settle_roulette(msg)
            elif type_url.endswith("/types.MessageExpireRoulette"):
                msg = MessageExpireRoulette(); msg.ParseFromString(request.tx.msg.value)
                return self._check_message_expire_roulette(msg)
            elif type_url.endswith("/types.MessageOpenDomino"):
                msg = MessageOpenDomino(); msg.ParseFromString(request.tx.msg.value)
                return self._check_message_open_domino(msg)
            elif type_url.endswith("/types.MessageJoinDomino"):
                msg = MessageJoinDomino(); msg.ParseFromString(request.tx.msg.value)
                return self._check_message_join_domino(msg)
            elif type_url.endswith("/types.MessageSettleDomino"):
                msg = MessageSettleDomino(); msg.ParseFromString(request.tx.msg.value)
                return self._check_message_settle_domino(msg)
            elif type_url.endswith("/types.MessageExpireDomino"):
                msg = MessageExpireDomino(); msg.ParseFromString(request.tx.msg.value)
                return self._check_message_expire_domino(msg)
            else:
                raise err_invalid_message_cast()

        except PluginError as e:
            response = PluginCheckResponse()
            response.error.code = e.code
            response.error.module = e.module
            response.error.msg = e.msg
            return response
        except Exception as err:
            response = PluginCheckResponse()
            response.error.code = 1
            response.error.module = "plugin"
            response.error.msg = str(err)
            return response

    async def deliver_tx(self, request: PluginDeliverRequest) -> PluginDeliverResponse:
        """DeliverTx is code that is executed to apply a transaction."""
        try:
            # Get the message and handle by type
            type_url = request.tx.msg.type_url
            if type_url.endswith("/types.MessageSend"):
                msg = MessageSend()
                msg.ParseFromString(request.tx.msg.value)
                return await self._deliver_message_send(msg, request.tx.fee, request.tx.memo)
            elif type_url.endswith("/types.MessageFaucet"):
                msg = MessageFaucet()
                msg.ParseFromString(request.tx.msg.value)
                return await self._deliver_message_faucet(msg)
            elif type_url.endswith("/types.MessageReward"):
                msg = MessageReward()
                msg.ParseFromString(request.tx.msg.value)
                return await self._deliver_message_reward(msg, request.tx.fee)
            elif type_url.endswith("/types.MessageOpenRoom"):
                msg = MessageOpenRoom()
                msg.ParseFromString(request.tx.msg.value)
                return await self._deliver_message_open_room(msg, request.height)
            elif type_url.endswith("/types.MessageJoinRoom"):
                msg = MessageJoinRoom()
                msg.ParseFromString(request.tx.msg.value)
                return await self._deliver_message_join_room(msg)
            elif type_url.endswith("/types.MessageSettleRoom"):
                msg = MessageSettleRoom()
                msg.ParseFromString(request.tx.msg.value)
                return await self._deliver_message_settle_room(msg)
            elif type_url.endswith("/types.MessageExpireRoom"):
                msg = MessageExpireRoom()
                msg.ParseFromString(request.tx.msg.value)
                return await self._deliver_message_expire_room(msg, request.height)
            elif type_url.endswith("/types.MessageBuyCoins"):
                msg = MessageBuyCoins(); msg.ParseFromString(request.tx.msg.value)
                return await self._deliver_buy_coins(msg)
            elif type_url.endswith("/types.MessageBuyGems"):
                msg = MessageBuyGems(); msg.ParseFromString(request.tx.msg.value)
                return await self._deliver_buy_gems(msg)
            elif type_url.endswith("/types.MessageTransferGems"):
                msg = MessageTransferGems(); msg.ParseFromString(request.tx.msg.value)
                return await self._deliver_transfer_gems(msg)
            elif type_url.endswith("/types.MessageMintCosmetic"):
                msg = MessageMintCosmetic(); msg.ParseFromString(request.tx.msg.value)
                return await self._deliver_mint_cosmetic(msg)
            elif type_url.endswith("/types.MessageBuyCosmetic"):
                msg = MessageBuyCosmetic(); msg.ParseFromString(request.tx.msg.value)
                return await self._deliver_buy_cosmetic(msg)
            elif type_url.endswith("/types.MessageTransferCosmetic"):
                msg = MessageTransferCosmetic(); msg.ParseFromString(request.tx.msg.value)
                return await self._deliver_transfer_cosmetic(msg)
            elif type_url.endswith("/types.MessageOpenRoulette"):
                msg = MessageOpenRoulette(); msg.ParseFromString(request.tx.msg.value)
                return await self._deliver_message_open_roulette(msg, request.height)
            elif type_url.endswith("/types.MessageRouletteBet"):
                msg = MessageRouletteBet(); msg.ParseFromString(request.tx.msg.value)
                return await self._deliver_message_roulette_bet(msg)
            elif type_url.endswith("/types.MessageSettleRoulette"):
                msg = MessageSettleRoulette(); msg.ParseFromString(request.tx.msg.value)
                return await self._deliver_message_settle_roulette(msg)
            elif type_url.endswith("/types.MessageExpireRoulette"):
                msg = MessageExpireRoulette(); msg.ParseFromString(request.tx.msg.value)
                return await self._deliver_message_expire_roulette(msg, request.height)
            elif type_url.endswith("/types.MessageOpenDomino"):
                msg = MessageOpenDomino(); msg.ParseFromString(request.tx.msg.value)
                return await self._deliver_message_open_domino(msg, request.height)
            elif type_url.endswith("/types.MessageJoinDomino"):
                msg = MessageJoinDomino(); msg.ParseFromString(request.tx.msg.value)
                return await self._deliver_message_join_domino(msg)
            elif type_url.endswith("/types.MessageSettleDomino"):
                msg = MessageSettleDomino(); msg.ParseFromString(request.tx.msg.value)
                return await self._deliver_message_settle_domino(msg)
            elif type_url.endswith("/types.MessageExpireDomino"):
                msg = MessageExpireDomino(); msg.ParseFromString(request.tx.msg.value)
                return await self._deliver_message_expire_domino(msg, request.height)
            else:
                raise err_invalid_message_cast()

        except PluginError as e:
            response = PluginDeliverResponse()
            response.error.code = e.code
            response.error.module = e.module
            response.error.msg = e.msg
            return response
        except Exception as err:
            response = PluginDeliverResponse()
            response.error.code = 1
            response.error.module = "plugin"
            response.error.msg = str(err)
            return response

    def end_block(self, request: PluginEndRequest) -> PluginEndResponse:
        """EndBlock is code that is executed at the end of applying a block."""
        return PluginEndResponse()

    def _check_message_send(self, msg: MessageSend) -> PluginCheckResponse:
        """CheckMessageSend statelessly validates a 'send' message."""
        # Check sender address (must be exactly 20 bytes)
        if len(msg.from_address) != 20:
            raise err_invalid_address()

        # Check recipient address (must be exactly 20 bytes)
        if len(msg.to_address) != 20:
            raise err_invalid_address()

        # Check amount (must be greater than 0)
        if msg.amount == 0:
            raise err_invalid_amount()

        # Return authorized signers (sender must sign)
        response = PluginCheckResponse()
        response.recipient = msg.to_address
        response.authorized_signers.append(msg.from_address)
        return response

    async def _deliver_message_send(self, msg: MessageSend, fee: int, memo: str) -> PluginDeliverResponse:
        """DeliverMessageSend handles a 'send' message."""
        if not self.plugin or not self.config:
            raise PluginError(1, "plugin", "plugin or config not initialized")

        # Generate query IDs
        from_query_id = random.randint(0, 2**53)
        to_query_id = random.randint(0, 2**53)
        fee_query_id = random.randint(0, 2**53)

        # Calculate keys
        from_key = key_for_account(msg.from_address)
        to_key = key_for_account(msg.to_address)
        fee_pool_key = key_for_fee_pool(self.config.chain_id)

        # Get the from and to accounts
        response = await self.plugin.state_read(
            self,
            PluginStateReadRequest(
                keys=[
                    PluginKeyRead(query_id=fee_query_id, key=fee_pool_key),
                    PluginKeyRead(query_id=from_query_id, key=from_key),
                    PluginKeyRead(query_id=to_query_id, key=to_key),
                ]
            ),
        )

        # Check for internal error
        if response.HasField("error"):
            result = PluginDeliverResponse()
            result.error.CopyFrom(response.error)
            return result

        # Get the from bytes and to bytes
        from_bytes = None
        to_bytes = None
        fee_pool_bytes = None

        for resp in response.results:
            if resp.query_id == from_query_id:
                from_bytes = resp.entries[0].value if resp.entries else None
            elif resp.query_id == to_query_id:
                to_bytes = resp.entries[0].value if resp.entries else None
            elif resp.query_id == fee_query_id:
                fee_pool_bytes = resp.entries[0].value if resp.entries else None

        if msg.amount > UINT64_MAX - fee:
            raise err_invalid_amount()

        # Add fee to amount to deduct
        amount_to_deduct = msg.amount + fee

        # Convert bytes to account structures
        from_account = unmarshal(Account, from_bytes) if from_bytes else Account()
        to_account = unmarshal(Account, to_bytes) if to_bytes else Account()
        fee_pool = unmarshal(Pool, fee_pool_bytes) if fee_pool_bytes else Pool()

        # Check sufficient funds
        if from_account.amount < amount_to_deduct:
            raise err_insufficient_funds()

        # For self-transfer, use same account data
        if from_key == to_key:
            to_account = from_account

        if fee_pool.amount > UINT64_MAX - fee or (
            from_key != to_key and to_account.amount > UINT64_MAX - msg.amount
        ):
            raise err_invalid_amount()

        # Subtract from sender
        from_account.amount -= amount_to_deduct

        # Add the fee to the fee pool
        fee_pool.amount += fee

        # Add to recipient
        to_account.amount += msg.amount

        # Convert accounts to bytes
        from_bytes_new = marshal(from_account)
        to_bytes_new = marshal(to_account)
        fee_pool_bytes_new = marshal(fee_pool)

        # Retain drained accounts only when they carry nonce state or core will advance the nonce after RLP.V2 delivery.
        sets = [
            PluginSetOp(key=fee_pool_key, value=fee_pool_bytes_new),
            PluginSetOp(key=to_key, value=to_bytes_new),
        ]
        deletes = []
        if from_account.amount == 0 and from_account.nonce == 0 and memo != "RLP.V2":
            deletes.append(PluginDeleteOp(key=from_key))
        else:
            sets.append(PluginSetOp(key=from_key, value=from_bytes_new))
        write_resp = await self.plugin.state_write(
            self,
            PluginStateWriteRequest(
                sets=sets,
                deletes=deletes,
            ),
        )

        result = PluginDeliverResponse()
        if write_resp.HasField("error"):
            result.error.CopyFrom(write_resp.error)
        return result

    # ── faucet / reward (Phase 0 tutorial validation) ────────────────────────

    def _check_message_faucet(self, msg: MessageFaucet) -> PluginCheckResponse:
        """Statelessly validate a 'faucet' message (admin-only mint, no balance check)."""
        if len(msg.signer_address) != 20:
            raise err_invalid_address()
        if len(msg.recipient_address) != 20:
            raise err_invalid_address()
        if msg.amount == 0:
            raise err_invalid_amount()
        if msg.signer_address not in ADMIN_ADDRESSES:
            raise err_unauthorized_signer()
        response = PluginCheckResponse()
        response.recipient = msg.recipient_address
        response.authorized_signers.append(msg.signer_address)
        return response

    def _check_message_reward(self, msg: MessageReward) -> PluginCheckResponse:
        """Statelessly validate a 'reward' message (admin-authorised mint)."""
        if len(msg.admin_address) != 20:
            raise err_invalid_address()
        if len(msg.recipient_address) != 20:
            raise err_invalid_address()
        if msg.amount == 0:
            raise err_invalid_amount()
        if msg.admin_address not in ADMIN_ADDRESSES:
            raise err_unauthorized_signer()
        response = PluginCheckResponse()
        response.recipient = msg.recipient_address
        response.authorized_signers.append(msg.admin_address)
        return response

    async def _deliver_message_faucet(self, msg: MessageFaucet) -> PluginDeliverResponse:
        """Mint tokens to recipient (no balance check, no fee) and track a Faucet record."""
        if not self.plugin or not self.config:
            raise PluginError(1, "plugin", "plugin or config not initialized")
        acct_qid = random.randint(0, 2**53)
        rec_qid = random.randint(0, 2**53)
        acct_key = key_for_account(msg.recipient_address)
        rec_key = key_for_faucet(msg.recipient_address)
        response = await self.plugin.state_read(
            self,
            PluginStateReadRequest(keys=[
                PluginKeyRead(query_id=acct_qid, key=acct_key),
                PluginKeyRead(query_id=rec_qid, key=rec_key),
            ]),
        )
        if response.HasField("error"):
            result = PluginDeliverResponse()
            result.error.CopyFrom(response.error)
            return result
        acct_bytes = rec_bytes = None
        for r in response.results:
            if r.query_id == acct_qid:
                acct_bytes = r.entries[0].value if r.entries else None
            elif r.query_id == rec_qid:
                rec_bytes = r.entries[0].value if r.entries else None
        account = unmarshal(Account, acct_bytes) if acct_bytes else Account()
        record = unmarshal(Faucet, rec_bytes) if rec_bytes else Faucet()
        if account.amount > UINT64_MAX - msg.amount:
            raise err_invalid_amount()
        account.amount += msg.amount
        record.recipient_address = msg.recipient_address
        record.total_amount += msg.amount
        record.count += 1
        write_resp = await self.plugin.state_write(
            self,
            PluginStateWriteRequest(sets=[
                PluginSetOp(key=acct_key, value=marshal(account)),
                PluginSetOp(key=rec_key, value=marshal(record)),
            ]),
        )
        result = PluginDeliverResponse()
        if write_resp.HasField("error"):
            result.error.CopyFrom(write_resp.error)
        return result

    async def _deliver_message_reward(self, msg: MessageReward, fee: int) -> PluginDeliverResponse:
        """Admin pays the fee; mint tokens to recipient and track a Reward record."""
        if not self.plugin or not self.config:
            raise PluginError(1, "plugin", "plugin or config not initialized")
        admin_qid = random.randint(0, 2**53)
        rec_qid = random.randint(0, 2**53)
        fee_qid = random.randint(0, 2**53)
        rrec_qid = random.randint(0, 2**53)
        admin_key = key_for_account(msg.admin_address)
        rec_key = key_for_account(msg.recipient_address)
        fee_pool_key = key_for_fee_pool(self.config.chain_id)
        rrec_key = key_for_reward(msg.recipient_address)
        response = await self.plugin.state_read(
            self,
            PluginStateReadRequest(keys=[
                PluginKeyRead(query_id=fee_qid, key=fee_pool_key),
                PluginKeyRead(query_id=admin_qid, key=admin_key),
                PluginKeyRead(query_id=rec_qid, key=rec_key),
                PluginKeyRead(query_id=rrec_qid, key=rrec_key),
            ]),
        )
        if response.HasField("error"):
            result = PluginDeliverResponse()
            result.error.CopyFrom(response.error)
            return result
        admin_bytes = rec_bytes = fee_pool_bytes = rrec_bytes = None
        for r in response.results:
            if r.query_id == admin_qid:
                admin_bytes = r.entries[0].value if r.entries else None
            elif r.query_id == rec_qid:
                rec_bytes = r.entries[0].value if r.entries else None
            elif r.query_id == fee_qid:
                fee_pool_bytes = r.entries[0].value if r.entries else None
            elif r.query_id == rrec_qid:
                rrec_bytes = r.entries[0].value if r.entries else None
        admin_account = unmarshal(Account, admin_bytes) if admin_bytes else Account()
        recipient_account = unmarshal(Account, rec_bytes) if rec_bytes else Account()
        fee_pool = unmarshal(Pool, fee_pool_bytes) if fee_pool_bytes else Pool()
        record = unmarshal(Reward, rrec_bytes) if rrec_bytes else Reward()
        if admin_account.amount < fee:
            raise err_insufficient_funds()
        if admin_key == rec_key:
            recipient_account = admin_account
        if recipient_account.amount > UINT64_MAX - msg.amount or fee_pool.amount > UINT64_MAX - fee:
            raise err_invalid_amount()
        admin_account.amount -= fee
        recipient_account.amount += msg.amount
        fee_pool.amount += fee
        record.recipient_address = msg.recipient_address
        record.last_admin_address = msg.admin_address
        record.total_amount += msg.amount
        record.count += 1
        sets = [
            PluginSetOp(key=fee_pool_key, value=marshal(fee_pool)),
            PluginSetOp(key=rec_key, value=marshal(recipient_account)),
            PluginSetOp(key=admin_key, value=marshal(admin_account)),
            PluginSetOp(key=rrec_key, value=marshal(record)),
        ]
        write_resp = await self.plugin.state_write(self, PluginStateWriteRequest(sets=sets))
        result = PluginDeliverResponse()
        if write_resp.HasField("error"):
            result.error.CopyFrom(write_resp.error)
        return result

    # ── Bingo Rush room escrow (commit / reveal, trustless multi-rank) ───────

    def _check_message_open_room(self, msg) -> PluginCheckResponse:
        if len(msg.operator_address) != 20:
            raise err_invalid_address()
        if not msg.round_id:
            raise PluginError(1, "plugin", "empty round_id")
        if len(msg.commitment) != 32:
            raise PluginError(1, "plugin", "commitment must be 32 bytes")
        if msg.entry_fee == 0:
            raise err_invalid_amount()
        if msg.rake_bps > 10000:
            raise PluginError(1, "plugin", "rake_bps must be <= 10000")
        if len(msg.payout_weights_bps) > 0 and sum(msg.payout_weights_bps) != 10000:
            raise PluginError(1, "plugin", "payout_weights_bps must sum to 10000")
        r = PluginCheckResponse()
        r.authorized_signers.append(msg.operator_address)
        return r

    def _check_message_join_room(self, msg) -> PluginCheckResponse:
        if len(msg.player_address) != 20:
            raise err_invalid_address()
        if not msg.round_id:
            raise PluginError(1, "plugin", "empty round_id")
        if not 1 <= msg.num_cards <= 4:
            raise PluginError(1, "plugin", "num_cards must be 1..4")
        if msg.amount == 0:
            raise err_invalid_amount()
        r = PluginCheckResponse()
        r.authorized_signers.append(msg.player_address)
        return r

    def _check_message_settle_room(self, msg) -> PluginCheckResponse:
        if len(msg.operator_address) != 20:
            raise err_invalid_address()
        if not msg.round_id:
            raise PluginError(1, "plugin", "empty round_id")
        if not msg.seed:
            raise PluginError(1, "plugin", "empty seed")
        r = PluginCheckResponse()
        r.authorized_signers.append(msg.operator_address)
        return r

    def _check_message_expire_room(self, msg) -> PluginCheckResponse:
        """Statelessly validate an 'expire_room' message. Anyone may call this
        (they just pay their own tx fee) -- the deliver step is what actually
        enforces the round exists, is still open, and has passed its deadline."""
        if len(msg.caller_address) != 20:
            raise err_invalid_address()
        if not msg.round_id:
            raise PluginError(1, "plugin", "empty round_id")
        r = PluginCheckResponse()
        r.authorized_signers.append(msg.caller_address)
        return r

    async def _deliver_message_open_room(self, msg, height: int = 0) -> PluginDeliverResponse:
        if not self.plugin or not self.config:
            raise PluginError(1, "plugin", "plugin or config not initialized")
        round_key = key_for_round(msg.round_id)
        val, err = await self._read_one(round_key)
        if err:
            out = PluginDeliverResponse(); out.error.CopyFrom(err); return out
        if val:
            raise PluginError(1, "plugin", "round already exists")
        rr = RoomRound()
        rr.round_id = msg.round_id
        rr.operator_address = msg.operator_address
        rr.commitment = msg.commitment
        rr.entry_fee = msg.entry_fee
        rr.rake_bps = msg.rake_bps
        rr.escrow_total = 0
        rr.num_players = 0
        rr.status = 0
        rr.payout_weights_bps.extend(list(msg.payout_weights_bps) or [10000])
        rr.opened_height = height
        w = await self.plugin.state_write(self, PluginStateWriteRequest(
            sets=[PluginSetOp(key=round_key, value=marshal(rr))]))
        out = PluginDeliverResponse()
        if w.HasField("error"):
            out.error.CopyFrom(w.error)
        return out

    async def _deliver_message_expire_room(self, msg, height: int = 0) -> PluginDeliverResponse:
        """Refund every escrowed entry for a round the operator never settled
        within ROOM_EXPIRY_BLOCKS. Each participant gets back exactly what
        their own RoomParticipant record says they put in -- not a recomputed
        guess -- so this can't over- or under-pay regardless of who calls it."""
        if not self.plugin or not self.config:
            raise PluginError(1, "plugin", "plugin or config not initialized")
        round_key = key_for_round(msg.round_id)
        val, err = await self._read_one(round_key)
        if err:
            out = PluginDeliverResponse(); out.error.CopyFrom(err); return out
        rr = unmarshal(RoomRound, val) if val else None
        if rr is None:
            raise PluginError(1, "plugin", "round not found")
        if rr.status != 0:
            raise PluginError(1, "plugin", "round is not open")
        if height < rr.opened_height + ROOM_EXPIRY_BLOCKS:
            raise err_room_not_expired()

        participant_addrs = [bytes(a) for a in rr.participant_addresses]
        escrow_key = key_for_account(escrow_address(msg.round_id))
        qe = random.randint(0, 2**53)
        keys = [PluginKeyRead(query_id=qe, key=escrow_key)]
        part_qids, acct_qids = [], {}
        for addr in participant_addrs:
            pq = random.randint(0, 2**53)
            aq = random.randint(0, 2**53)
            part_qids.append((pq, addr))
            acct_qids[addr] = aq
            keys.append(PluginKeyRead(query_id=pq, key=key_for_participant(msg.round_id, addr)))
            keys.append(PluginKeyRead(query_id=aq, key=key_for_account(addr)))
        resp = await self.plugin.state_read(self, PluginStateReadRequest(keys=keys))
        if resp.HasField("error"):
            out = PluginDeliverResponse(); out.error.CopyFrom(resp.error); return out
        by_qid = {}
        for r in resp.results:
            by_qid[r.query_id] = r.entries[0].value if r.entries else None

        escrow = unmarshal(Account, by_qid.get(qe)) if by_qid.get(qe) else Account()
        sets = []
        total_refunded = 0
        for pq, addr in part_qids:
            part_bytes = by_qid.get(pq)
            part = unmarshal(RoomParticipant, part_bytes) if part_bytes else None
            amount = part.amount if part else 0
            if amount <= 0:
                continue
            acct_bytes = by_qid.get(acct_qids[addr])
            acct = unmarshal(Account, acct_bytes) if acct_bytes else Account()
            acct.amount += amount
            total_refunded += amount
            sets.append(PluginSetOp(key=key_for_account(addr), value=marshal(acct)))

        if escrow.amount < total_refunded:
            raise PluginError(1, "plugin", "escrow underfunded")
        escrow.amount -= total_refunded
        sets.append(PluginSetOp(key=escrow_key, value=marshal(escrow)))
        rr.status = 2  # expired/refunded
        sets.append(PluginSetOp(key=round_key, value=marshal(rr)))

        w = await self.plugin.state_write(self, PluginStateWriteRequest(sets=sets))
        out = PluginDeliverResponse()
        if w.HasField("error"):
            out.error.CopyFrom(w.error)
        return out

    async def _deliver_message_join_room(self, msg) -> PluginDeliverResponse:
        if not self.plugin or not self.config:
            raise PluginError(1, "plugin", "plugin or config not initialized")
        round_key = key_for_round(msg.round_id)
        part_key = key_for_participant(msg.round_id, msg.player_address)
        player_key = key_for_account(msg.player_address)
        escrow_key = key_for_account(escrow_address(msg.round_id))
        qr, qp, qpl, qe = (random.randint(0, 2**53) for _ in range(4))
        resp = await self.plugin.state_read(self, PluginStateReadRequest(keys=[
            PluginKeyRead(query_id=qr, key=round_key),
            PluginKeyRead(query_id=qp, key=part_key),
            PluginKeyRead(query_id=qpl, key=player_key),
            PluginKeyRead(query_id=qe, key=escrow_key),
        ]))
        if resp.HasField("error"):
            out = PluginDeliverResponse(); out.error.CopyFrom(resp.error); return out
        rb = pb = plb = eb = None
        for r in resp.results:
            if r.query_id == qr:
                rb = r.entries[0].value if r.entries else None
            elif r.query_id == qp:
                pb = r.entries[0].value if r.entries else None
            elif r.query_id == qpl:
                plb = r.entries[0].value if r.entries else None
            elif r.query_id == qe:
                eb = r.entries[0].value if r.entries else None
        rr = unmarshal(RoomRound, rb) if rb else None
        if rr is None:
            raise PluginError(1, "plugin", "round not found")
        if rr.status != 0:
            raise PluginError(1, "plugin", "round not open")
        if pb:
            raise PluginError(1, "plugin", "player already joined")
        player = unmarshal(Account, plb) if plb else Account()
        escrow = unmarshal(Account, eb) if eb else Account()
        if player.amount < msg.amount:
            raise err_insufficient_funds()
        player.amount -= msg.amount
        escrow.amount += msg.amount
        rr.escrow_total += msg.amount
        rr.num_players += 1
        rr.participant_addresses.append(msg.player_address)
        rr.participant_num_cards.append(msg.num_cards)
        part = RoomParticipant()
        part.round_id = msg.round_id
        part.player_address = msg.player_address
        part.num_cards = msg.num_cards
        part.amount = msg.amount
        w = await self.plugin.state_write(self, PluginStateWriteRequest(sets=[
            PluginSetOp(key=player_key, value=marshal(player)),
            PluginSetOp(key=escrow_key, value=marshal(escrow)),
            PluginSetOp(key=round_key, value=marshal(rr)),
            PluginSetOp(key=part_key, value=marshal(part)),
        ]))
        out = PluginDeliverResponse()
        if w.HasField("error"):
            out.error.CopyFrom(w.error)
        return out

    async def _deliver_message_settle_room(self, msg) -> PluginDeliverResponse:
        """Trustless settle: operator only reveals the seed. The plugin recomputes
        every participant's cards from the seed, ranks the winners by who completes
        the pattern first, and pays the top ranks by the round's payout weights."""
        if not self.plugin or not self.config:
            raise PluginError(1, "plugin", "plugin or config not initialized")
        round_key = key_for_round(msg.round_id)
        val, err = await self._read_one(round_key)
        if err:
            out = PluginDeliverResponse(); out.error.CopyFrom(err); return out
        rr = unmarshal(RoomRound, val) if val else None
        if rr is None:
            raise PluginError(1, "plugin", "round not found")
        if rr.status != 0:
            raise PluginError(1, "plugin", "round already settled")
        if bytes(rr.operator_address) != bytes(msg.operator_address):
            raise PluginError(1, "plugin", "only the operator can settle")
        if seed_commitment(bytes(msg.seed)) != bytes(rr.commitment):
            raise PluginError(1, "plugin", "seed does not match commitment")
        # recompute the ranking from the revealed seed (fully determined on-chain)
        seed = bytes(msg.seed)
        order = gdraw.draw_order(seed)
        pattern = grules.Pattern(msg.pattern or "line")
        ranked = []
        for i in range(len(rr.participant_addresses)):
            addr = bytes(rr.participant_addresses[i])
            n = rr.participant_num_cards[i]
            cards = gcard.generate_cards(derive_seed(seed, addr), n)
            idxs = [grules.first_win_index(c, order, pattern) for c in cards]
            idxs = [x for x in idxs if x > 0]
            if idxs:
                ranked.append((min(idxs), addr))
        if not ranked:
            raise PluginError(1, "plugin", "no winner")
        ranked.sort(key=lambda t: (t[0], t[1]))
        # weights: truncate to number of winners, renormalise to 10000 bps
        weights = list(rr.payout_weights_bps) or [10000]
        winners = [addr for _, addr in ranked][:len(weights)]
        weights = weights[:len(winners)]
        tot = sum(weights)
        if tot != 10000:
            weights = [w * 10000 // tot for w in weights]
            weights[0] += 10000 - sum(weights)
        total = rr.escrow_total
        rake = total * rr.rake_bps // 10000
        net = total - rake
        payouts = [net * w // 10000 for w in weights]
        payouts[0] += net - sum(payouts)  # remainder to the top rank
        # read escrow, fee pool and every winner account
        escrow_key = key_for_account(escrow_address(msg.round_id))
        treasury_key = key_for_account(TREASURY_ADDRESS)
        qe = random.randint(0, 2**53)
        qf = random.randint(0, 2**53)
        keys = [PluginKeyRead(query_id=qe, key=escrow_key),
                PluginKeyRead(query_id=qf, key=treasury_key)]
        winner_qids = []
        for addr in winners:
            q = random.randint(0, 2**53)
            winner_qids.append((q, addr))
            keys.append(PluginKeyRead(query_id=q, key=key_for_account(addr)))
        resp = await self.plugin.state_read(self, PluginStateReadRequest(keys=keys))
        if resp.HasField("error"):
            out = PluginDeliverResponse(); out.error.CopyFrom(resp.error); return out
        by_qid = {}
        for r in resp.results:
            by_qid[r.query_id] = r.entries[0].value if r.entries else None
        escrow = unmarshal(Account, by_qid.get(qe)) if by_qid.get(qe) else Account()
        treasury = unmarshal(Account, by_qid.get(qf)) if by_qid.get(qf) else Account()
        if escrow.amount < total:
            raise PluginError(1, "plugin", "escrow underfunded")
        escrow.amount -= total
        treasury.amount += rake
        sets = [PluginSetOp(key=escrow_key, value=marshal(escrow)),
                PluginSetOp(key=treasury_key, value=marshal(treasury))]
        for i, (q, addr) in enumerate(winner_qids):
            acct = unmarshal(Account, by_qid.get(q)) if by_qid.get(q) else Account()
            acct.amount += payouts[i]
            sets.append(PluginSetOp(key=key_for_account(addr), value=marshal(acct)))
        rr.status = 1
        sets.append(PluginSetOp(key=round_key, value=marshal(rr)))
        w = await self.plugin.state_write(self, PluginStateWriteRequest(sets=sets))
        out = PluginDeliverResponse()
        if w.HasField("error"):
            out.error.CopyFrom(w.error)
        return out

    # ── Roulette round escrow (commit / reveal, single wheel per round) ──────

    def _check_message_open_roulette(self, msg) -> PluginCheckResponse:
        if len(msg.operator_address) != 20:
            raise err_invalid_address()
        if not msg.round_id:
            raise PluginError(1, "plugin", "empty round_id")
        if len(msg.commitment) != 32:
            raise PluginError(1, "plugin", "commitment must be 32 bytes")
        if msg.rake_bps > 10000:
            raise PluginError(1, "plugin", "rake_bps must be <= 10000")
        # Unlike open_room, this is gated to admins from day one -- open_room's
        # unrestricted operator field is a known gap (any address can claim to
        # "operate" a room), not something to carry into a new message type.
        if msg.operator_address not in ADMIN_ADDRESSES:
            raise err_unauthorized_signer()
        r = PluginCheckResponse()
        r.authorized_signers.append(msg.operator_address)
        return r

    def _check_message_roulette_bet(self, msg) -> PluginCheckResponse:
        if len(msg.player_address) != 20:
            raise err_invalid_address()
        if not msg.round_id:
            raise PluginError(1, "plugin", "empty round_id")
        if msg.amount == 0:
            raise err_invalid_amount()
        if not groulette.is_valid_bet(msg.bet_type, msg.bet_number):
            raise PluginError(1, "plugin", "invalid bet_type/bet_number")
        r = PluginCheckResponse()
        r.authorized_signers.append(msg.player_address)
        return r

    def _check_message_settle_roulette(self, msg) -> PluginCheckResponse:
        if len(msg.operator_address) != 20:
            raise err_invalid_address()
        if not msg.round_id:
            raise PluginError(1, "plugin", "empty round_id")
        if not msg.seed:
            raise PluginError(1, "plugin", "empty seed")
        r = PluginCheckResponse()
        r.authorized_signers.append(msg.operator_address)
        return r

    def _check_message_expire_roulette(self, msg) -> PluginCheckResponse:
        """Statelessly validate an 'expire_roulette' message. Anyone may call
        this -- the deliver step enforces the round exists, is still open, and
        has passed its deadline."""
        if len(msg.caller_address) != 20:
            raise err_invalid_address()
        if not msg.round_id:
            raise PluginError(1, "plugin", "empty round_id")
        r = PluginCheckResponse()
        r.authorized_signers.append(msg.caller_address)
        return r

    async def _deliver_message_open_roulette(self, msg, height: int = 0) -> PluginDeliverResponse:
        if not self.plugin or not self.config:
            raise PluginError(1, "plugin", "plugin or config not initialized")
        round_key = key_for_roulette_round(msg.round_id)
        val, err = await self._read_one(round_key)
        if err:
            out = PluginDeliverResponse(); out.error.CopyFrom(err); return out
        if val:
            raise PluginError(1, "plugin", "round already exists")
        rr = RouletteRound()
        rr.round_id = msg.round_id
        rr.operator_address = msg.operator_address
        rr.commitment = msg.commitment
        rr.rake_bps = msg.rake_bps
        rr.pot_total = 0
        rr.status = 0
        rr.opened_height = height
        w = await self.plugin.state_write(self, PluginStateWriteRequest(
            sets=[PluginSetOp(key=round_key, value=marshal(rr))]))
        out = PluginDeliverResponse()
        if w.HasField("error"):
            out.error.CopyFrom(w.error)
        return out

    async def _deliver_message_roulette_bet(self, msg) -> PluginDeliverResponse:
        if not self.plugin or not self.config:
            raise PluginError(1, "plugin", "plugin or config not initialized")
        round_key = key_for_roulette_round(msg.round_id)
        bet_key = key_for_roulette_bet(msg.round_id, msg.player_address)
        player_key = key_for_account(msg.player_address)
        escrow_key = key_for_account(roulette_escrow_address(msg.round_id))
        qr, qb, qpl, qe = (random.randint(0, 2**53) for _ in range(4))
        resp = await self.plugin.state_read(self, PluginStateReadRequest(keys=[
            PluginKeyRead(query_id=qr, key=round_key),
            PluginKeyRead(query_id=qb, key=bet_key),
            PluginKeyRead(query_id=qpl, key=player_key),
            PluginKeyRead(query_id=qe, key=escrow_key),
        ]))
        if resp.HasField("error"):
            out = PluginDeliverResponse(); out.error.CopyFrom(resp.error); return out
        rb = bb = plb = eb = None
        for r in resp.results:
            if r.query_id == qr:
                rb = r.entries[0].value if r.entries else None
            elif r.query_id == qb:
                bb = r.entries[0].value if r.entries else None
            elif r.query_id == qpl:
                plb = r.entries[0].value if r.entries else None
            elif r.query_id == qe:
                eb = r.entries[0].value if r.entries else None
        rr = unmarshal(RouletteRound, rb) if rb else None
        if rr is None:
            raise PluginError(1, "plugin", "round not found")
        if rr.status != 0:
            raise PluginError(1, "plugin", "round not open")
        if bb:
            raise PluginError(1, "plugin", "address already has a bet in this round")
        player = unmarshal(Account, plb) if plb else Account()
        escrow = unmarshal(Account, eb) if eb else Account()
        if player.amount < msg.amount:
            raise err_insufficient_funds()
        player.amount -= msg.amount
        escrow.amount += msg.amount
        rr.pot_total += msg.amount
        rr.bettor_addresses.append(msg.player_address)
        bet = RouletteBetRecord()
        bet.round_id = msg.round_id
        bet.player_address = msg.player_address
        bet.bet_type = msg.bet_type
        bet.bet_number = msg.bet_number
        bet.amount = msg.amount
        w = await self.plugin.state_write(self, PluginStateWriteRequest(sets=[
            PluginSetOp(key=player_key, value=marshal(player)),
            PluginSetOp(key=escrow_key, value=marshal(escrow)),
            PluginSetOp(key=round_key, value=marshal(rr)),
            PluginSetOp(key=bet_key, value=marshal(bet)),
        ]))
        out = PluginDeliverResponse()
        if w.HasField("error"):
            out.error.CopyFrom(w.error)
        return out

    async def _deliver_message_settle_roulette(self, msg) -> PluginDeliverResponse:
        """Trustless settle: operator only reveals the seed. The plugin
        recomputes the spin from the seed and pays every bettor whose bet
        matches -- exactly like Bingo's settle recomputes cards rather than
        trusting a claimed outcome. Unlike Bingo (where the whole pot is split
        among ranked winners), each roulette bettor is paid independently off
        their own bet's odds; whatever the escrow doesn't pay out (losing
        stakes) plus the rake skimmed off winning payouts both go to the
        treasury, so escrow always ends a settle at exactly zero."""
        if not self.plugin or not self.config:
            raise PluginError(1, "plugin", "plugin or config not initialized")
        round_key = key_for_roulette_round(msg.round_id)
        val, err = await self._read_one(round_key)
        if err:
            out = PluginDeliverResponse(); out.error.CopyFrom(err); return out
        rr = unmarshal(RouletteRound, val) if val else None
        if rr is None:
            raise PluginError(1, "plugin", "round not found")
        if rr.status != 0:
            raise PluginError(1, "plugin", "round already settled")
        if bytes(rr.operator_address) != bytes(msg.operator_address):
            raise PluginError(1, "plugin", "only the operator can settle")
        if seed_commitment(bytes(msg.seed)) != bytes(rr.commitment):
            raise PluginError(1, "plugin", "seed does not match commitment")

        spin = groulette.spin_number(bytes(msg.seed))
        bettors = [bytes(a) for a in rr.bettor_addresses]

        escrow_key = key_for_account(roulette_escrow_address(msg.round_id))
        treasury_key = key_for_account(TREASURY_ADDRESS)
        qe = random.randint(0, 2**53)
        qf = random.randint(0, 2**53)
        keys = [PluginKeyRead(query_id=qe, key=escrow_key),
                PluginKeyRead(query_id=qf, key=treasury_key)]
        bet_qids, acct_qids = [], {}
        for addr in bettors:
            bq = random.randint(0, 2**53)
            aq = random.randint(0, 2**53)
            bet_qids.append((bq, addr))
            acct_qids[addr] = aq
            keys.append(PluginKeyRead(query_id=bq, key=key_for_roulette_bet(msg.round_id, addr)))
            keys.append(PluginKeyRead(query_id=aq, key=key_for_account(addr)))
        resp = await self.plugin.state_read(self, PluginStateReadRequest(keys=keys))
        if resp.HasField("error"):
            out = PluginDeliverResponse(); out.error.CopyFrom(resp.error); return out
        by_qid = {}
        for r in resp.results:
            by_qid[r.query_id] = r.entries[0].value if r.entries else None

        escrow = unmarshal(Account, by_qid.get(qe)) if by_qid.get(qe) else Account()
        treasury = unmarshal(Account, by_qid.get(qf)) if by_qid.get(qf) else Account()

        sets = []
        total_gross_to_winners = 0
        total_rake = 0
        for bq, addr in bet_qids:
            bet_bytes = by_qid.get(bq)
            bet = unmarshal(RouletteBetRecord, bet_bytes) if bet_bytes else None
            if bet is None:
                continue
            gross = groulette.payout_for(bet.bet_type, bet.bet_number, bet.amount, spin)
            if gross <= 0:
                continue  # lost -- their stake stays in escrow and becomes house profit below
            rake = gross * rr.rake_bps // 10000
            net = gross - rake
            acct_bytes = by_qid.get(acct_qids[addr])
            acct = unmarshal(Account, acct_bytes) if acct_bytes else Account()
            acct.amount += net
            total_gross_to_winners += gross
            total_rake += rake
            sets.append(PluginSetOp(key=key_for_account(addr), value=marshal(acct)))

        # Fixed-odds payouts (a straight-up win pays 36x) routinely exceed what
        # this one round collected -- unlike Bingo's pari-mutuel split, the
        # pot can't be assumed to cover its own payouts. The round's escrow
        # pays first; the treasury (accumulated rake + house_take from every
        # other round) backstops any shortfall, exactly like a real casino
        # bankroll. If the treasury itself can't cover it, settle fails safely
        # rather than paying out from nothing.
        if escrow.amount >= total_gross_to_winners:
            house_take = escrow.amount - total_gross_to_winners  # losing stakes -> pure house profit
            treasury.amount += total_rake + house_take
        else:
            shortfall = total_gross_to_winners - escrow.amount
            if treasury.amount < shortfall:
                raise PluginError(1, "plugin", "treasury underfunded to cover payout")
            treasury.amount += total_rake - shortfall
        escrow.amount = 0
        sets.append(PluginSetOp(key=escrow_key, value=marshal(escrow)))
        sets.append(PluginSetOp(key=treasury_key, value=marshal(treasury)))

        rr.status = 1
        rr.seed = msg.seed
        rr.spin = spin
        sets.append(PluginSetOp(key=round_key, value=marshal(rr)))

        w = await self.plugin.state_write(self, PluginStateWriteRequest(sets=sets))
        out = PluginDeliverResponse()
        if w.HasField("error"):
            out.error.CopyFrom(w.error)
        return out

    async def _deliver_message_expire_roulette(self, msg, height: int = 0) -> PluginDeliverResponse:
        """Refund every escrowed bet for a round the operator never settled
        within ROOM_EXPIRY_BLOCKS. Each bettor gets back exactly what their own
        RouletteBetRecord says they staked -- not a recomputed guess."""
        if not self.plugin or not self.config:
            raise PluginError(1, "plugin", "plugin or config not initialized")
        round_key = key_for_roulette_round(msg.round_id)
        val, err = await self._read_one(round_key)
        if err:
            out = PluginDeliverResponse(); out.error.CopyFrom(err); return out
        rr = unmarshal(RouletteRound, val) if val else None
        if rr is None:
            raise PluginError(1, "plugin", "round not found")
        if rr.status != 0:
            raise PluginError(1, "plugin", "round is not open")
        if height < rr.opened_height + ROOM_EXPIRY_BLOCKS:
            raise err_room_not_expired()

        bettors = [bytes(a) for a in rr.bettor_addresses]
        escrow_key = key_for_account(roulette_escrow_address(msg.round_id))
        qe = random.randint(0, 2**53)
        keys = [PluginKeyRead(query_id=qe, key=escrow_key)]
        bet_qids, acct_qids = [], {}
        for addr in bettors:
            bq = random.randint(0, 2**53)
            aq = random.randint(0, 2**53)
            bet_qids.append((bq, addr))
            acct_qids[addr] = aq
            keys.append(PluginKeyRead(query_id=bq, key=key_for_roulette_bet(msg.round_id, addr)))
            keys.append(PluginKeyRead(query_id=aq, key=key_for_account(addr)))
        resp = await self.plugin.state_read(self, PluginStateReadRequest(keys=keys))
        if resp.HasField("error"):
            out = PluginDeliverResponse(); out.error.CopyFrom(resp.error); return out
        by_qid = {}
        for r in resp.results:
            by_qid[r.query_id] = r.entries[0].value if r.entries else None
        escrow = unmarshal(Account, by_qid.get(qe)) if by_qid.get(qe) else Account()

        sets = []
        total_refunded = 0
        for bq, addr in bet_qids:
            bet_bytes = by_qid.get(bq)
            bet = unmarshal(RouletteBetRecord, bet_bytes) if bet_bytes else None
            amount = bet.amount if bet else 0
            if amount <= 0:
                continue
            acct_bytes = by_qid.get(acct_qids[addr])
            acct = unmarshal(Account, acct_bytes) if acct_bytes else Account()
            acct.amount += amount
            total_refunded += amount
            sets.append(PluginSetOp(key=key_for_account(addr), value=marshal(acct)))

        if escrow.amount < total_refunded:
            raise PluginError(1, "plugin", "escrow underfunded")
        escrow.amount -= total_refunded
        sets.append(PluginSetOp(key=escrow_key, value=marshal(escrow)))
        rr.status = 2  # expired/refunded
        sets.append(PluginSetOp(key=round_key, value=marshal(rr)))

        w = await self.plugin.state_write(self, PluginStateWriteRequest(sets=sets))
        out = PluginDeliverResponse()
        if w.HasField("error"):
            out.error.CopyFrom(w.error)
        return out

    # ── Domino round escrow (heads-up, commit-reveal deal + replayed moves) ──

    def _check_message_open_domino(self, msg) -> PluginCheckResponse:
        if len(msg.operator_address) != 20:
            raise err_invalid_address()
        if not msg.round_id:
            raise PluginError(1, "plugin", "empty round_id")
        if len(msg.commitment) != 32:
            raise PluginError(1, "plugin", "commitment must be 32 bytes")
        if msg.entry_fee == 0:
            raise err_invalid_amount()
        if msg.rake_bps > 10000:
            raise PluginError(1, "plugin", "rake_bps must be <= 10000")
        if msg.operator_address not in ADMIN_ADDRESSES:
            raise err_unauthorized_signer()
        r = PluginCheckResponse()
        r.authorized_signers.append(msg.operator_address)
        return r

    def _check_message_join_domino(self, msg) -> PluginCheckResponse:
        if len(msg.player_address) != 20:
            raise err_invalid_address()
        if not msg.round_id:
            raise PluginError(1, "plugin", "empty round_id")
        if msg.amount == 0:
            raise err_invalid_amount()
        r = PluginCheckResponse()
        r.authorized_signers.append(msg.player_address)
        return r

    def _check_message_settle_domino(self, msg) -> PluginCheckResponse:
        if len(msg.operator_address) != 20:
            raise err_invalid_address()
        if not msg.round_id:
            raise PluginError(1, "plugin", "empty round_id")
        if not msg.seed:
            raise PluginError(1, "plugin", "empty seed")
        if not msg.moves:
            raise PluginError(1, "plugin", "empty move log")
        r = PluginCheckResponse()
        r.authorized_signers.append(msg.operator_address)
        return r

    def _check_message_expire_domino(self, msg) -> PluginCheckResponse:
        """Statelessly validate an 'expire_domino' message. Anyone may call
        this -- the deliver step enforces the round exists, is still open,
        and has passed its deadline."""
        if len(msg.caller_address) != 20:
            raise err_invalid_address()
        if not msg.round_id:
            raise PluginError(1, "plugin", "empty round_id")
        r = PluginCheckResponse()
        r.authorized_signers.append(msg.caller_address)
        return r

    async def _deliver_message_open_domino(self, msg, height: int = 0) -> PluginDeliverResponse:
        if not self.plugin or not self.config:
            raise PluginError(1, "plugin", "plugin or config not initialized")
        round_key = key_for_domino_round(msg.round_id)
        val, err = await self._read_one(round_key)
        if err:
            out = PluginDeliverResponse(); out.error.CopyFrom(err); return out
        if val:
            raise PluginError(1, "plugin", "round already exists")
        rr = DominoRound()
        rr.round_id = msg.round_id
        rr.operator_address = msg.operator_address
        rr.commitment = msg.commitment
        rr.entry_fee = msg.entry_fee
        rr.rake_bps = msg.rake_bps
        rr.escrow_total = 0
        rr.status = 0
        rr.opened_height = height
        w = await self.plugin.state_write(self, PluginStateWriteRequest(
            sets=[PluginSetOp(key=round_key, value=marshal(rr))]))
        out = PluginDeliverResponse()
        if w.HasField("error"):
            out.error.CopyFrom(w.error)
        return out

    async def _deliver_message_join_domino(self, msg) -> PluginDeliverResponse:
        if not self.plugin or not self.config:
            raise PluginError(1, "plugin", "plugin or config not initialized")
        round_key = key_for_domino_round(msg.round_id)
        player_key = key_for_account(msg.player_address)
        escrow_key = key_for_account(domino_escrow_address(msg.round_id))
        qr, qpl, qe = (random.randint(0, 2**53) for _ in range(3))
        resp = await self.plugin.state_read(self, PluginStateReadRequest(keys=[
            PluginKeyRead(query_id=qr, key=round_key),
            PluginKeyRead(query_id=qpl, key=player_key),
            PluginKeyRead(query_id=qe, key=escrow_key),
        ]))
        if resp.HasField("error"):
            out = PluginDeliverResponse(); out.error.CopyFrom(resp.error); return out
        rb = plb = eb = None
        for r in resp.results:
            if r.query_id == qr:
                rb = r.entries[0].value if r.entries else None
            elif r.query_id == qpl:
                plb = r.entries[0].value if r.entries else None
            elif r.query_id == qe:
                eb = r.entries[0].value if r.entries else None
        rr = unmarshal(DominoRound, rb) if rb else None
        if rr is None:
            raise PluginError(1, "plugin", "round not found")
        if rr.status != 0:
            raise PluginError(1, "plugin", "round not open")
        if len(rr.participant_addresses) >= gdomino.NUM_PLAYERS:
            raise PluginError(1, "plugin", "round already has enough players")
        if bytes(msg.player_address) in [bytes(a) for a in rr.participant_addresses]:
            raise PluginError(1, "plugin", "address already joined")
        if msg.amount != rr.entry_fee:
            raise PluginError(1, "plugin", "amount must equal the round's entry fee")
        player = unmarshal(Account, plb) if plb else Account()
        escrow = unmarshal(Account, eb) if eb else Account()
        if player.amount < msg.amount:
            raise err_insufficient_funds()
        player.amount -= msg.amount
        escrow.amount += msg.amount
        rr.escrow_total += msg.amount
        rr.participant_addresses.append(msg.player_address)
        w = await self.plugin.state_write(self, PluginStateWriteRequest(sets=[
            PluginSetOp(key=player_key, value=marshal(player)),
            PluginSetOp(key=escrow_key, value=marshal(escrow)),
            PluginSetOp(key=round_key, value=marshal(rr)),
        ]))
        out = PluginDeliverResponse()
        if w.HasField("error"):
            out.error.CopyFrom(w.error)
        return out

    async def _deliver_message_settle_domino(self, msg) -> PluginDeliverResponse:
        """Trustless settle: operator reveals the seed AND the full move log.
        The plugin replays both (deal derived from the seed, each move
        validated legal in sequence) to derive the winner itself -- exactly
        like Bingo/Roulette recompute their outcome from the seed, extended
        here to also validate the moves since domino's outcome depends on
        player choices, not just the seed. An illegal move anywhere in the
        claimed log rejects the whole settle -- there is no partial credit."""
        if not self.plugin or not self.config:
            raise PluginError(1, "plugin", "plugin or config not initialized")
        round_key = key_for_domino_round(msg.round_id)
        val, err = await self._read_one(round_key)
        if err:
            out = PluginDeliverResponse(); out.error.CopyFrom(err); return out
        rr = unmarshal(DominoRound, val) if val else None
        if rr is None:
            raise PluginError(1, "plugin", "round not found")
        if rr.status != 0:
            raise PluginError(1, "plugin", "round already settled")
        if bytes(rr.operator_address) != bytes(msg.operator_address):
            raise PluginError(1, "plugin", "only the operator can settle")
        if seed_commitment(bytes(msg.seed)) != bytes(rr.commitment):
            raise PluginError(1, "plugin", "seed does not match commitment")
        if len(rr.participant_addresses) != gdomino.NUM_PLAYERS:
            raise PluginError(1, "plugin", "round never filled both seats")

        moves = [
            gdomino.Move(action=m.action,
                         tile=(m.tile_low, m.tile_high) if m.action == "play" else None,
                         end=m.end or None)
            for m in msg.moves
        ]
        try:
            result = gdomino.replay(bytes(msg.seed), moves)
        except gdomino.IllegalMove as exc:
            raise PluginError(1, "plugin", f"illegal move in claimed log: {exc}")

        participants = [bytes(a) for a in rr.participant_addresses]
        winners = [participants[i] for i in result.winners]

        total = rr.escrow_total
        rake = total * rr.rake_bps // 10000
        net = total - rake
        per_winner = net // len(winners)
        payouts = [per_winner] * len(winners)
        payouts[0] += net - sum(payouts)  # remainder to the first winner

        escrow_key = key_for_account(domino_escrow_address(msg.round_id))
        treasury_key = key_for_account(TREASURY_ADDRESS)
        qe = random.randint(0, 2**53)
        qf = random.randint(0, 2**53)
        keys = [PluginKeyRead(query_id=qe, key=escrow_key),
                PluginKeyRead(query_id=qf, key=treasury_key)]
        winner_qids = []
        for addr in winners:
            q = random.randint(0, 2**53)
            winner_qids.append((q, addr))
            keys.append(PluginKeyRead(query_id=q, key=key_for_account(addr)))
        resp = await self.plugin.state_read(self, PluginStateReadRequest(keys=keys))
        if resp.HasField("error"):
            out = PluginDeliverResponse(); out.error.CopyFrom(resp.error); return out
        by_qid = {}
        for r in resp.results:
            by_qid[r.query_id] = r.entries[0].value if r.entries else None
        escrow = unmarshal(Account, by_qid.get(qe)) if by_qid.get(qe) else Account()
        treasury = unmarshal(Account, by_qid.get(qf)) if by_qid.get(qf) else Account()
        if escrow.amount < total:
            raise PluginError(1, "plugin", "escrow underfunded")
        escrow.amount -= total
        treasury.amount += rake
        sets = [PluginSetOp(key=escrow_key, value=marshal(escrow)),
                PluginSetOp(key=treasury_key, value=marshal(treasury))]
        for i, (q, addr) in enumerate(winner_qids):
            acct = unmarshal(Account, by_qid.get(q)) if by_qid.get(q) else Account()
            acct.amount += payouts[i]
            sets.append(PluginSetOp(key=key_for_account(addr), value=marshal(acct)))

        rr.status = 1
        rr.seed = msg.seed
        rr.winner_slots.extend(result.winners)
        rr.result_reason = result.reason
        sets.append(PluginSetOp(key=round_key, value=marshal(rr)))

        w = await self.plugin.state_write(self, PluginStateWriteRequest(sets=sets))
        out = PluginDeliverResponse()
        if w.HasField("error"):
            out.error.CopyFrom(w.error)
        return out

    async def _deliver_message_expire_domino(self, msg, height: int = 0) -> PluginDeliverResponse:
        """Refund every escrowed entry fee for a round the operator never
        settled within ROOM_EXPIRY_BLOCKS -- including a round that only ever
        got one participant (that one address is simply refunded alone)."""
        if not self.plugin or not self.config:
            raise PluginError(1, "plugin", "plugin or config not initialized")
        round_key = key_for_domino_round(msg.round_id)
        val, err = await self._read_one(round_key)
        if err:
            out = PluginDeliverResponse(); out.error.CopyFrom(err); return out
        rr = unmarshal(DominoRound, val) if val else None
        if rr is None:
            raise PluginError(1, "plugin", "round not found")
        if rr.status != 0:
            raise PluginError(1, "plugin", "round is not open")
        if height < rr.opened_height + ROOM_EXPIRY_BLOCKS:
            raise err_room_not_expired()

        participants = [bytes(a) for a in rr.participant_addresses]
        escrow_key = key_for_account(domino_escrow_address(msg.round_id))
        qe = random.randint(0, 2**53)
        keys = [PluginKeyRead(query_id=qe, key=escrow_key)]
        acct_qids = {}
        for addr in participants:
            aq = random.randint(0, 2**53)
            acct_qids[addr] = aq
            keys.append(PluginKeyRead(query_id=aq, key=key_for_account(addr)))
        resp = await self.plugin.state_read(self, PluginStateReadRequest(keys=keys))
        if resp.HasField("error"):
            out = PluginDeliverResponse(); out.error.CopyFrom(resp.error); return out
        by_qid = {}
        for r in resp.results:
            by_qid[r.query_id] = r.entries[0].value if r.entries else None
        escrow = unmarshal(Account, by_qid.get(qe)) if by_qid.get(qe) else Account()

        sets = []
        total_refunded = 0
        for addr in participants:
            acct_bytes = by_qid.get(acct_qids[addr])
            acct = unmarshal(Account, acct_bytes) if acct_bytes else Account()
            acct.amount += rr.entry_fee
            total_refunded += rr.entry_fee
            sets.append(PluginSetOp(key=key_for_account(addr), value=marshal(acct)))

        if escrow.amount < total_refunded:
            raise PluginError(1, "plugin", "escrow underfunded")
        escrow.amount -= total_refunded
        sets.append(PluginSetOp(key=escrow_key, value=marshal(escrow)))
        rr.status = 2  # expired/refunded
        sets.append(PluginSetOp(key=round_key, value=marshal(rr)))

        w = await self.plugin.state_write(self, PluginStateWriteRequest(sets=sets))
        out = PluginDeliverResponse()
        if w.HasField("error"):
            out.error.CopyFrom(w.error)
        return out

    # ── economy: coins/gems ───────────────────────────────────────────────────

    def _check_mint_like(self, admin, recipient, amount):
        """Shared check for buy_coins/buy_gems — both mint unconditionally to
        `recipient` with no balance debit, so `admin` must be a real admin."""
        if len(admin) != 20 or len(recipient) != 20:
            raise err_invalid_address()
        if amount == 0:
            raise err_invalid_amount()
        if admin not in ADMIN_ADDRESSES:
            raise err_unauthorized_signer()
        r = PluginCheckResponse()
        r.recipient = recipient
        r.authorized_signers.append(admin)
        return r

    def _check_transfer_gems(self, msg):
        if len(msg.from_address) != 20 or len(msg.to_address) != 20:
            raise err_invalid_address()
        if msg.amount == 0:
            raise err_invalid_amount()
        r = PluginCheckResponse()
        r.recipient = msg.to_address
        r.authorized_signers.append(msg.from_address)
        return r

    async def _read_one(self, key):
        qid = random.randint(0, 2**53)
        resp = await self.plugin.state_read(self, PluginStateReadRequest(
            keys=[PluginKeyRead(query_id=qid, key=key)]))
        if resp.HasField("error"):
            return None, resp.error
        val = None
        for r in resp.results:
            if r.query_id == qid:
                val = r.entries[0].value if r.entries else None
        return val, None

    async def _deliver_buy_coins(self, msg):
        if not self.plugin or not self.config:
            raise PluginError(1, "plugin", "plugin or config not initialized")
        key = key_for_account(msg.recipient_address)
        val, err = await self._read_one(key)
        if err:
            out = PluginDeliverResponse(); out.error.CopyFrom(err); return out
        acct = unmarshal(Account, val) if val else Account()
        if acct.amount > UINT64_MAX - msg.amount:
            raise err_invalid_amount()
        acct.amount += msg.amount
        w = await self.plugin.state_write(self, PluginStateWriteRequest(
            sets=[PluginSetOp(key=key, value=marshal(acct))]))
        out = PluginDeliverResponse()
        if w.HasField("error"):
            out.error.CopyFrom(w.error)
        return out

    async def _deliver_buy_gems(self, msg):
        if not self.plugin or not self.config:
            raise PluginError(1, "plugin", "plugin or config not initialized")
        key = key_for_gems(msg.recipient_address)
        val, err = await self._read_one(key)
        if err:
            out = PluginDeliverResponse(); out.error.CopyFrom(err); return out
        gb = unmarshal(GemBalance, val) if val else GemBalance()
        if gb.amount > UINT64_MAX - msg.amount:
            raise err_invalid_amount()
        gb.address = msg.recipient_address
        gb.amount += msg.amount
        w = await self.plugin.state_write(self, PluginStateWriteRequest(
            sets=[PluginSetOp(key=key, value=marshal(gb))]))
        out = PluginDeliverResponse()
        if w.HasField("error"):
            out.error.CopyFrom(w.error)
        return out

    async def _deliver_transfer_gems(self, msg):
        if not self.plugin or not self.config:
            raise PluginError(1, "plugin", "plugin or config not initialized")
        from_key = key_for_gems(msg.from_address)
        to_key = key_for_gems(msg.to_address)
        qf, qt = random.randint(0, 2**53), random.randint(0, 2**53)
        resp = await self.plugin.state_read(self, PluginStateReadRequest(keys=[
            PluginKeyRead(query_id=qf, key=from_key),
            PluginKeyRead(query_id=qt, key=to_key),
        ]))
        if resp.HasField("error"):
            out = PluginDeliverResponse(); out.error.CopyFrom(resp.error); return out
        fb = tb = None
        for r in resp.results:
            if r.query_id == qf:
                fb = r.entries[0].value if r.entries else None
            elif r.query_id == qt:
                tb = r.entries[0].value if r.entries else None
        src = unmarshal(GemBalance, fb) if fb else GemBalance()
        dst = unmarshal(GemBalance, tb) if tb else GemBalance()
        if src.amount < msg.amount:
            raise err_insufficient_funds()
        if from_key == to_key:
            dst = src
        if dst.amount > UINT64_MAX - msg.amount:
            raise err_invalid_amount()
        src.address = msg.from_address
        dst.address = msg.to_address
        src.amount -= msg.amount
        dst.amount += msg.amount
        w = await self.plugin.state_write(self, PluginStateWriteRequest(sets=[
            PluginSetOp(key=from_key, value=marshal(src)),
            PluginSetOp(key=to_key, value=marshal(dst)),
        ]))
        out = PluginDeliverResponse()
        if w.HasField("error"):
            out.error.CopyFrom(w.error)
        return out

    # ── NFT cosmetics ─────────────────────────────────────────────────────────

    def _check_mint_cosmetic(self, msg):
        if len(msg.operator_address) != 20 or len(msg.owner_address) != 20:
            raise err_invalid_address()
        if not msg.token_id:
            raise PluginError(1, "plugin", "empty token_id")
        if not msg.kind:
            raise PluginError(1, "plugin", "empty kind")
        r = PluginCheckResponse()
        r.recipient = msg.owner_address
        r.authorized_signers.append(msg.operator_address)
        return r

    def _check_buy_cosmetic(self, msg):
        if len(msg.player_address) != 20:
            raise err_invalid_address()
        if not msg.token_id:
            raise PluginError(1, "plugin", "empty token_id")
        if not msg.kind:
            raise PluginError(1, "plugin", "empty kind")
        r = PluginCheckResponse()
        r.authorized_signers.append(msg.player_address)
        return r

    def _check_transfer_cosmetic(self, msg):
        if len(msg.from_address) != 20 or len(msg.to_address) != 20:
            raise err_invalid_address()
        if not msg.token_id:
            raise PluginError(1, "plugin", "empty token_id")
        r = PluginCheckResponse()
        r.recipient = msg.to_address
        r.authorized_signers.append(msg.from_address)
        return r

    async def _deliver_mint_cosmetic(self, msg):
        if not self.plugin or not self.config:
            raise PluginError(1, "plugin", "plugin or config not initialized")
        key = key_for_cosmetic(msg.token_id)
        val, err = await self._read_one(key)
        if err:
            out = PluginDeliverResponse(); out.error.CopyFrom(err); return out
        if val:
            raise PluginError(1, "plugin", "token already exists")
        cos = Cosmetic()
        cos.token_id = msg.token_id
        cos.kind = msg.kind
        cos.owner_address = msg.owner_address
        w = await self.plugin.state_write(self, PluginStateWriteRequest(
            sets=[PluginSetOp(key=key, value=marshal(cos))]))
        out = PluginDeliverResponse()
        if w.HasField("error"):
            out.error.CopyFrom(w.error)
        return out

    async def _deliver_buy_cosmetic(self, msg):
        if not self.plugin or not self.config:
            raise PluginError(1, "plugin", "plugin or config not initialized")
        # price comes from the SHARED economy catalog (must be a gem-priced item)
        try:
            item = gecon.get_shop_item(msg.kind)
        except ValueError:
            raise PluginError(1, "plugin", "unknown cosmetic kind")
        if item.price_kind != gecon.PriceKind.GEMS:
            raise PluginError(1, "plugin", "cosmetic not purchasable with gems")
        cost = item.price
        cos_key = key_for_cosmetic(msg.token_id)
        gem_key = key_for_gems(msg.player_address)
        qc, qg = random.randint(0, 2**53), random.randint(0, 2**53)
        resp = await self.plugin.state_read(self, PluginStateReadRequest(keys=[
            PluginKeyRead(query_id=qc, key=cos_key),
            PluginKeyRead(query_id=qg, key=gem_key),
        ]))
        if resp.HasField("error"):
            out = PluginDeliverResponse(); out.error.CopyFrom(resp.error); return out
        cb = gb = None
        for r in resp.results:
            if r.query_id == qc:
                cb = r.entries[0].value if r.entries else None
            elif r.query_id == qg:
                gb = r.entries[0].value if r.entries else None
        if cb:
            raise PluginError(1, "plugin", "token already exists")
        gems = unmarshal(GemBalance, gb) if gb else GemBalance()
        if gems.amount < cost:
            raise err_insufficient_funds()
        gems.address = msg.player_address
        gems.amount -= cost
        cos = Cosmetic()
        cos.token_id = msg.token_id
        cos.kind = msg.kind
        cos.owner_address = msg.player_address
        w = await self.plugin.state_write(self, PluginStateWriteRequest(sets=[
            PluginSetOp(key=gem_key, value=marshal(gems)),
            PluginSetOp(key=cos_key, value=marshal(cos)),
        ]))
        out = PluginDeliverResponse()
        if w.HasField("error"):
            out.error.CopyFrom(w.error)
        return out

    async def _deliver_transfer_cosmetic(self, msg):
        if not self.plugin or not self.config:
            raise PluginError(1, "plugin", "plugin or config not initialized")
        key = key_for_cosmetic(msg.token_id)
        val, err = await self._read_one(key)
        if err:
            out = PluginDeliverResponse(); out.error.CopyFrom(err); return out
        if not val:
            raise PluginError(1, "plugin", "token not found")
        cos = unmarshal(Cosmetic, val)
        if bytes(cos.owner_address) != bytes(msg.from_address):
            raise PluginError(1, "plugin", "sender does not own token")
        cos.owner_address = msg.to_address
        w = await self.plugin.state_write(self, PluginStateWriteRequest(
            sets=[PluginSetOp(key=key, value=marshal(cos))]))
        out = PluginDeliverResponse()
        if w.HasField("error"):
            out.error.CopyFrom(w.error)
        return out
