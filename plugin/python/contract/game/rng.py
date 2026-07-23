"""Deterministic, seed-driven RNG for provably-fair Bingo.

Everything random in the game (card layout, ball draw order) is derived from a
single ``seed`` through this RNG. Given the same seed, every machine — the game
server AND the on-chain plugin re-verifying a result — produces the exact same
output. That is what makes the game auditable: the server commits to
``hash(seed)`` before a round and reveals ``seed`` after, and anyone can replay
the round to confirm the balls and the winner.

Why not ``random.Random``? Mersenne Twister's mapping from seed to stream is an
implementation detail that can differ across languages/runtimes. Here we build
the stream from SHA-256 of ``seed || counter`` — a construction that is trivial
to reproduce in any language, so a future non-Python verifier still agrees.
"""

from __future__ import annotations

import hashlib
from typing import List, Sequence, TypeVar

T = TypeVar("T")


class DeterministicRNG:
    """A reproducible stream of randomness keyed by ``seed`` (bytes).

    Bytes are produced as ``SHA256(seed || counter_be8)`` blocks, concatenated
    and consumed left to right. Integers are drawn with rejection sampling to
    avoid modulo bias.
    """

    def __init__(self, seed: bytes):
        if not isinstance(seed, (bytes, bytearray)):
            raise TypeError("seed must be bytes")
        self._seed = bytes(seed)
        self._counter = 0
        self._buffer = bytearray()

    @classmethod
    def from_text(cls, seed: str) -> "DeterministicRNG":
        return cls(seed.encode("utf-8"))

    def _refill(self) -> None:
        block = hashlib.sha256(self._seed + self._counter.to_bytes(8, "big")).digest()
        self._counter += 1
        self._buffer.extend(block)

    def _next_bytes(self, n: int) -> bytes:
        while len(self._buffer) < n:
            self._refill()
        out = bytes(self._buffer[:n])
        del self._buffer[:n]
        return out

    def randbelow(self, n: int) -> int:
        """Uniform integer in [0, n) with no modulo bias."""
        if n <= 0:
            raise ValueError("n must be positive")
        if n == 1:
            return 0
        # smallest byte count that covers n, plus rejection sampling
        num_bytes = (n.bit_length() + 7) // 8
        limit = (1 << (8 * num_bytes)) - ((1 << (8 * num_bytes)) % n)
        while True:
            val = int.from_bytes(self._next_bytes(num_bytes), "big")
            if val < limit:
                return val % n

    def shuffle(self, items: Sequence[T]) -> List[T]:
        """Return a new list, Fisher–Yates shuffled deterministically."""
        result = list(items)
        for i in range(len(result) - 1, 0, -1):
            j = self.randbelow(i + 1)
            result[i], result[j] = result[j], result[i]
        return result


def derive_seed(*parts: bytes) -> bytes:
    """Combine entropy sources into one 32-byte seed (e.g. server_seed ||
    block_hash || room_id). Order matters and must be fixed by the protocol."""
    h = hashlib.sha256()
    for p in parts:
        if isinstance(p, str):
            p = p.encode("utf-8")
        h.update(len(p).to_bytes(4, "big"))  # length-prefix so parts can't run together
        h.update(p)
    return h.digest()


def commitment(seed: bytes) -> bytes:
    """The value published on-chain BEFORE a round; ``seed`` is revealed after.
    Verifiers check ``commitment(revealed_seed) == published_commitment``."""
    return hashlib.sha256(seed).digest()
