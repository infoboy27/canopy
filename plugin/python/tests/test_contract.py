"""
Unit tests for the Contract class.

Covers the current `contract.contract` API: lifecycle hooks, stateless message
validation for the base 'send' transaction, and the tutorial 'faucet'/'reward'
transactions.
"""

import pytest

from contract.contract import Contract, ADMIN_ADDRESSES
from contract.plugin import Config
from contract.error import PluginError
from contract.proto import (
    MessageSend,
    MessageFaucet,
    MessageReward,
    PluginGenesisRequest,
    PluginBeginRequest,
    PluginEndRequest,
    PluginCheckRequest,
    PluginStateWriteResponse,
)

# Error codes (see contract/error.py)
CODE_INVALID_ADDRESS = 12
CODE_INVALID_AMOUNT = 13
CODE_UNAUTHORIZED_SIGNER = 15

ADDR_A = b"a" * 20
ADDR_B = b"b" * 20
ADDR_SHORT = b"short"
ADDR_ADMIN = next(iter(ADMIN_ADDRESSES))


class _CapturePlugin:
    """Records the last state_write; no read backing."""

    def __init__(self):
        self.last_write = None

    async def state_write(self, contract, request):
        self.last_write = request
        return PluginStateWriteResponse()


class _DictPlugin:
    """state_read/state_write backed by a plain dict (key -> value bytes)."""

    def __init__(self):
        self.kv = {}

    async def state_read(self, contract, request):
        from contract.proto import (
            PluginStateReadResponse, PluginReadResult, PluginStateEntry,
        )
        resp = PluginStateReadResponse()
        for kr in request.keys:
            r = PluginReadResult(query_id=kr.query_id)
            v = self.kv.get(bytes(kr.key))
            if v is not None:
                r.entries.append(PluginStateEntry(key=kr.key, value=v))
            resp.results.append(r)
        return resp

    async def state_write(self, contract, request):
        for op in request.sets:
            self.kv[bytes(op.key)] = op.value
        for op in request.deletes:
            self.kv.pop(bytes(op.key), None)
        return PluginStateWriteResponse()


@pytest.fixture
def config():
    """Default plugin configuration."""
    return Config()


@pytest.fixture
def contract(config):
    """Contract instance with config but no live plugin (stateless tests)."""
    return Contract(config=config)


class TestContractLifecycle:
    """Lifecycle hooks should succeed without error."""

    def test_genesis(self, contract):
        result = contract.genesis(PluginGenesisRequest())
        assert not result.HasField("error")

    async def test_begin_block(self, contract):
        result = await contract.begin_block(PluginBeginRequest())
        assert not result.HasField("error")

    async def test_begin_block_persists_fsm_authenticated_predecessor_hash(self, config):
        plugin = _CapturePlugin()
        contract = Contract(config=config, plugin=plugin, fsm_id=7)
        block_hash = b"h" * 32
        result = await contract.begin_block(
            PluginBeginRequest(height=12, last_block_hash=block_hash)
        )
        assert not result.HasField("error")
        assert len(plugin.last_write.sets) == 1
        # ledger value is hash||vdf; vdf empty in this phase
        assert plugin.last_write.sets[0].value == block_hash
        # keyed by the PREDECESSOR height (H-1)
        assert plugin.last_write.sets[0].key.endswith((11).to_bytes(8, "big"))
        assert len(plugin.last_write.deletes) == 0

    async def test_begin_block_prunes_beyond_retention(self, config):
        from contract.contract import CONSENSUS_ENTROPY_RETENTION, key_for_consensus_entropy

        plugin = _CapturePlugin()
        contract = Contract(config=config, plugin=plugin, fsm_id=7)
        height = CONSENSUS_ENTROPY_RETENTION + 50
        await contract.begin_block(
            PluginBeginRequest(height=height, last_block_hash=b"h" * 32)
        )
        assert len(plugin.last_write.deletes) == 1
        stale = (height - 1) - CONSENSUS_ENTROPY_RETENTION
        assert plugin.last_write.deletes[0].key == key_for_consensus_entropy(stale)

    async def test_begin_block_rejects_missing_consensus_hash_after_genesis(self, config):
        contract = Contract(config=config, plugin=_CapturePlugin(), fsm_id=7)
        result = await contract.begin_block(PluginBeginRequest(height=12))
        assert result.HasField("error")
        assert "consensus last block hash" in result.error.msg

    async def test_fold_consensus_entropy_is_deterministic_and_order_sensitive(self, config):
        from contract.contract import key_for_consensus_entropy, fold_consensus_entropy

        plugin = _DictPlugin()
        contract = Contract(config=config, plugin=plugin, fsm_id=7)
        hashes = {h: bytes([h]) * 32 for h in range(100, 108)}
        for h, v in hashes.items():
            plugin.kv[key_for_consensus_entropy(h)] = v

        entropy, err = await contract._fold_consensus_entropy(100, 107)
        assert err is None
        assert entropy == fold_consensus_entropy([hashes[h] for h in range(100, 108)])
        # a different window order yields different entropy
        assert entropy != fold_consensus_entropy([hashes[h] for h in range(107, 99, -1)])

    async def test_fold_consensus_entropy_fails_closed_on_gap(self, config):
        from contract.contract import key_for_consensus_entropy

        plugin = _DictPlugin()
        contract = Contract(config=config, plugin=plugin, fsm_id=7)
        for h in (100, 101, 103, 104):  # 102 missing
            plugin.kv[key_for_consensus_entropy(h)] = bytes([h]) * 32

        entropy, err = await contract._fold_consensus_entropy(100, 104)
        assert entropy is None
        assert err is not None and "height 102" in err.msg

    def test_end_block(self, contract):
        result = contract.end_block(PluginEndRequest())
        assert not result.HasField("error")


class TestCheckMessageSend:
    """Stateless validation of the base 'send' message."""

    def test_valid(self, contract):
        msg = MessageSend(from_address=ADDR_A, to_address=ADDR_B, amount=1000)
        result = contract._check_message_send(msg)

        assert not result.HasField("error")
        assert result.recipient == ADDR_B
        assert list(result.authorized_signers) == [ADDR_A]

    def test_invalid_from_address(self, contract):
        msg = MessageSend(from_address=ADDR_SHORT, to_address=ADDR_B, amount=1000)
        with pytest.raises(PluginError) as exc:
            contract._check_message_send(msg)
        assert exc.value.code == CODE_INVALID_ADDRESS

    def test_invalid_to_address(self, contract):
        msg = MessageSend(from_address=ADDR_A, to_address=ADDR_SHORT, amount=1000)
        with pytest.raises(PluginError) as exc:
            contract._check_message_send(msg)
        assert exc.value.code == CODE_INVALID_ADDRESS

    def test_invalid_amount(self, contract):
        msg = MessageSend(from_address=ADDR_A, to_address=ADDR_B, amount=0)
        with pytest.raises(PluginError) as exc:
            contract._check_message_send(msg)
        assert exc.value.code == CODE_INVALID_AMOUNT


class TestCheckMessageFaucet:
    """Stateless validation of the 'faucet' message (admin-only mint)."""

    def test_valid(self, contract):
        msg = MessageFaucet(signer_address=ADDR_ADMIN, recipient_address=ADDR_B, amount=500)
        result = contract._check_message_faucet(msg)

        assert not result.HasField("error")
        assert result.recipient == ADDR_B
        assert list(result.authorized_signers) == [ADDR_ADMIN]

    def test_invalid_recipient(self, contract):
        msg = MessageFaucet(signer_address=ADDR_ADMIN, recipient_address=ADDR_SHORT, amount=500)
        with pytest.raises(PluginError) as exc:
            contract._check_message_faucet(msg)
        assert exc.value.code == CODE_INVALID_ADDRESS

    def test_invalid_amount(self, contract):
        msg = MessageFaucet(signer_address=ADDR_ADMIN, recipient_address=ADDR_B, amount=0)
        with pytest.raises(PluginError) as exc:
            contract._check_message_faucet(msg)
        assert exc.value.code == CODE_INVALID_AMOUNT

    def test_unauthorized_signer_rejected(self, contract):
        """Regression: any address used to be able to mint via faucet by
        naming itself as signer — only an ADMIN_ADDRESSES entry may now."""
        msg = MessageFaucet(signer_address=ADDR_A, recipient_address=ADDR_B, amount=500)
        with pytest.raises(PluginError) as exc:
            contract._check_message_faucet(msg)
        assert exc.value.code == CODE_UNAUTHORIZED_SIGNER


class TestCheckMessageReward:
    """Stateless validation of the 'reward' message (admin-authorised mint)."""

    def test_valid(self, contract):
        msg = MessageReward(admin_address=ADDR_ADMIN, recipient_address=ADDR_B, amount=750)
        result = contract._check_message_reward(msg)

        assert not result.HasField("error")
        assert result.recipient == ADDR_B
        assert list(result.authorized_signers) == [ADDR_ADMIN]

    def test_invalid_admin(self, contract):
        msg = MessageReward(admin_address=ADDR_SHORT, recipient_address=ADDR_B, amount=750)
        with pytest.raises(PluginError) as exc:
            contract._check_message_reward(msg)
        assert exc.value.code == CODE_INVALID_ADDRESS

    def test_invalid_amount(self, contract):
        msg = MessageReward(admin_address=ADDR_ADMIN, recipient_address=ADDR_B, amount=0)
        with pytest.raises(PluginError) as exc:
            contract._check_message_reward(msg)
        assert exc.value.code == CODE_INVALID_AMOUNT

    def test_unauthorized_signer_rejected(self, contract):
        """Regression: any address used to be able to mint via reward by
        naming itself as admin_address — only an ADMIN_ADDRESSES entry may now."""
        msg = MessageReward(admin_address=ADDR_A, recipient_address=ADDR_B, amount=750)
        with pytest.raises(PluginError) as exc:
            contract._check_message_reward(msg)
        assert exc.value.code == CODE_UNAUTHORIZED_SIGNER


class TestCheckMintLike:
    """Stateless validation shared by buy_coins/buy_gems — both mint
    unconditionally to `recipient`, so `admin` must be authorized."""

    def test_valid(self, contract):
        result = contract._check_mint_like(ADDR_ADMIN, ADDR_B, 1_000)
        assert not result.HasField("error")
        assert result.recipient == ADDR_B
        assert list(result.authorized_signers) == [ADDR_ADMIN]

    def test_unauthorized_signer_rejected(self, contract):
        """Regression: buy_coins/buy_gems had the exact same unauthenticated-
        mint bug as faucet — anyone naming themselves as admin could mint."""
        with pytest.raises(PluginError) as exc:
            contract._check_mint_like(ADDR_A, ADDR_B, 1_000)
        assert exc.value.code == CODE_UNAUTHORIZED_SIGNER


@pytest.mark.asyncio
class TestCheckTx:
    """check_tx wiring guards."""

    async def test_check_tx_without_plugin(self, config):
        """check_tx must fail gracefully when no plugin is wired in."""
        contract = Contract(config=config)  # plugin is None
        result = await contract.check_tx(PluginCheckRequest())

        assert result.HasField("error")
        assert "plugin or config not initialized" in result.error.msg
