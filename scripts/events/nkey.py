import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from dataclasses import dataclass, field

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

OPERATOR = 14 << 3
ACCOUNT = 0
USER = 20 << 3
SEED = 18 << 3
HEADER = {"typ": "JWT", "alg": "ed25519-nkey"}
CREDS = (
    "-----BEGIN NATS USER JWT-----\n{jwt}\n------END NATS USER JWT------\n\n"
    "-----BEGIN USER NKEY SEED-----\n{seed}\n------END USER NKEY SEED------\n"
)
BLOCK = re.compile(r"^-{3,}BEGIN [A-Z ]+-{3,}\n(\S+)\n-{3,}END ", re.MULTILINE)


class NkeyError(ValueError):
    pass


def crc16(data: bytes) -> int:
    crc = 0
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = (crc << 1 ^ 0x1021 if crc & 0x8000 else crc << 1) & 0xFFFF
    return crc


def _encode(raw: bytes) -> str:
    return base64.b32encode(raw + crc16(raw).to_bytes(2, "little")).decode().rstrip("=")


def _decode(text: str) -> bytes:
    try:
        raw = base64.b32decode(text + "=" * (-len(text) % 8))
    except ValueError as exc:
        raise NkeyError("an nkey must be base32 text") from exc
    if len(raw) < 3 or crc16(raw[:-2]) != int.from_bytes(raw[-2:], "little"):
        raise NkeyError("the nkey checksum does not match")
    return raw[:-2]


@dataclass(frozen=True)
class KeyPair:
    seed: str = field(repr=False)
    public: str

    def sign(self, data: bytes) -> bytes:
        return _private(self.seed).sign(data)


def _private(seed: str) -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(_decode(seed)[2:])


def from_raw(prefix: int, raw: bytes) -> KeyPair:
    seed = _encode(bytes([SEED | prefix >> 5, (prefix & 31) << 3]) + raw)
    public = _private(seed).public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return KeyPair(seed, _encode(bytes([prefix]) + public))


def create(prefix: int) -> KeyPair:
    return from_raw(prefix, os.urandom(32))


def from_seed(seed: str) -> KeyPair:
    raw = _decode(seed)
    if len(raw) != 34 or raw[0] & 0xF8 != SEED:
        raise NkeyError("not an nkey seed")
    return from_raw((raw[0] & 7) << 5 | raw[1] >> 3, raw[2:])


def derive(parent: KeyPair, prefix: int, label: str) -> KeyPair:
    return from_raw(prefix, hmac.new(_decode(parent.seed)[2:], label.encode(), hashlib.sha256).digest())


def b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _segment(value: dict) -> str:
    return b64(json.dumps(value, separators=(",", ":")).encode())


def encode_jwt(signer: KeyPair, subject: str, name: str, nats: dict) -> str:
    claims = {"jti": secrets.token_hex(16), "iat": int(time.time()), "iss": signer.public, "name": name}
    signing = f"{_segment(HEADER)}.{_segment({**claims, 'sub': subject, 'nats': nats})}"
    return f"{signing}.{b64(signer.sign(signing.encode()))}"


def creds(jwt: str, seed: str) -> str:
    return CREDS.format(jwt=jwt, seed=seed)


def parse_creds(text: str) -> tuple[str, KeyPair]:
    blocks = BLOCK.findall(text)
    if len(blocks) != 2:
        raise NkeyError("a NATS creds file holds one user JWT and one seed")
    return blocks[0], from_seed(blocks[1])
