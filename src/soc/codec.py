"""Encrypt every payload before it leaves the process (alerts, prompts, model answers).

Temporal stores Workflow inputs, Activity results, Update arguments and memos as payloads. A
PayloadCodec sits in the client's data converter, so workers and the API encrypt on the way
out and decrypt on the way in, and the Temporal server (or Temporal Cloud) only ever holds
ciphertext. Search attributes are not payloads: they stay readable so Visibility can filter.

Off by default. Set SOC_CODEC_KEY to a base64 32-byte key (`openssl rand -base64 32`) in every
process that talks to Temporal. The Temporal UI can show plaintext through the codec endpoint
the console API serves at /codec (start the UI with --ui-codec-endpoint http://localhost:8080/codec).
"""

from __future__ import annotations

import base64
import dataclasses
import os
from collections.abc import Sequence

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from temporalio.api.common.v1 import Payload
from temporalio.converter import DataConverter, PayloadCodec

ENCODING = b"binary/encrypted"
NONCE_BYTES = 12


class EncryptionCodec(PayloadCodec):
    """AES-256-GCM over the whole serialized payload (metadata included)."""

    def __init__(self, key: bytes, key_id: str = "soc-key-1") -> None:
        self.aes, self.key_id = AESGCM(key), key_id.encode()

    @classmethod
    def from_env(cls) -> EncryptionCodec | None:
        key = os.environ.get("SOC_CODEC_KEY", "")
        return cls(base64.b64decode(key)) if key else None

    async def encode(self, payloads: Sequence[Payload]) -> list[Payload]:
        return [
            Payload(
                metadata={"encoding": ENCODING, "encryption-key-id": self.key_id},
                data=self._encrypt(p.SerializeToString()),
            )
            for p in payloads
        ]

    async def decode(self, payloads: Sequence[Payload]) -> list[Payload]:
        return [
            Payload.FromString(self._decrypt(p.data))
            if p.metadata.get("encoding") == ENCODING
            else p  # written before encryption was turned on
            for p in payloads
        ]

    def _encrypt(self, data: bytes) -> bytes:
        nonce = os.urandom(NONCE_BYTES)
        return nonce + self.aes.encrypt(nonce, data, None)

    def _decrypt(self, data: bytes) -> bytes:
        return self.aes.decrypt(data[:NONCE_BYTES], data[NONCE_BYTES:], None)


def data_converter(codec: PayloadCodec | None = None) -> DataConverter:
    """The default JSON converter, plus encryption when a key is configured."""
    codec = codec or EncryptionCodec.from_env()
    return dataclasses.replace(DataConverter.default, payload_codec=codec)
