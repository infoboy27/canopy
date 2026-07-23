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
)
from .game import card as gcard, draw as gdraw, rules as grules, economy as gecon
from .game.rng import derive_seed, commitment as seed_commitment

from .error import (
    PluginError,
    err_invalid_address,
    err_invalid_amount,
    err_insufficient_funds,
    err_tx_fee_below_state_limit,
    err_invalid_message_cast,
    err_unmarshal,
)


# Plugin configuration (matching Go's ContractConfig)
CONTRACT_CONFIG = {
    "name": "python_plugin_contract",
    "id": 1,
    "version": 1,
    "supported_transactions": ["send", "faucet", "reward", "open_room", "join_room", "settle_room", "buy_coins", "buy_gems", "transfer_gems", "mint_cosmetic", "buy_cosmetic", "transfer_cosmetic"],
    "transaction_type_urls": [
        "type.googleapis.com/types.MessageSend",
        "type.googleapis.com/types.MessageFaucet",
        "type.googleapis.com/types.MessageReward",
        "type.googleapis.com/types.MessageOpenRoom",
        "type.googleapis.com/types.MessageJoinRoom",
        "type.googleapis.com/types.MessageSettleRoom",
        "type.googleapis.com/types.MessageBuyCoins",
        "type.googleapis.com/types.MessageBuyGems",
        "type.googleapis.com/types.MessageTransferGems",
        "type.googleapis.com/types.MessageMintCosmetic",
        "type.googleapis.com/types.MessageBuyCosmetic",
        "type.googleapis.com/types.MessageTransferCosmetic",
    ],
    "event_type_urls": [],
    "custom_state_prefixes": [b"\x64", b"\x65", b"\x6e", b"\x6f", b"\x70", b"\x71"],  # +112 gems,113 cosmetic
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
                return await self._deliver_message_open_room(msg)
            elif type_url.endswith("/types.MessageJoinRoom"):
                msg = MessageJoinRoom()
                msg.ParseFromString(request.tx.msg.value)
                return await self._deliver_message_join_room(msg)
            elif type_url.endswith("/types.MessageSettleRoom"):
                msg = MessageSettleRoom()
                msg.ParseFromString(request.tx.msg.value)
                return await self._deliver_message_settle_room(msg)
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
        """Statelessly validate a 'faucet' message (test-only mint, no balance check)."""
        if len(msg.signer_address) != 20:
            raise err_invalid_address()
        if len(msg.recipient_address) != 20:
            raise err_invalid_address()
        if msg.amount == 0:
            raise err_invalid_amount()
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

    async def _deliver_message_open_room(self, msg) -> PluginDeliverResponse:
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
        w = await self.plugin.state_write(self, PluginStateWriteRequest(
            sets=[PluginSetOp(key=round_key, value=marshal(rr))]))
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
        fee_pool_key = key_for_fee_pool(self.config.chain_id)
        qe = random.randint(0, 2**53)
        qf = random.randint(0, 2**53)
        keys = [PluginKeyRead(query_id=qe, key=escrow_key),
                PluginKeyRead(query_id=qf, key=fee_pool_key)]
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
        fee_pool = unmarshal(Pool, by_qid.get(qf)) if by_qid.get(qf) else Pool()
        if escrow.amount < total:
            raise PluginError(1, "plugin", "escrow underfunded")
        escrow.amount -= total
        fee_pool.amount += rake
        sets = [PluginSetOp(key=escrow_key, value=marshal(escrow)),
                PluginSetOp(key=fee_pool_key, value=marshal(fee_pool))]
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

    # ── economy: coins/gems ───────────────────────────────────────────────────

    def _check_mint_like(self, admin, recipient, amount):
        if len(admin) != 20 or len(recipient) != 20:
            raise err_invalid_address()
        if amount == 0:
            raise err_invalid_amount()
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
