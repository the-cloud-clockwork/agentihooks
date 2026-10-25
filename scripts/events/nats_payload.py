import hashlib
import json
import secrets
import zlib
from collections.abc import Awaitable, Callable

from nats.aio.client import Client
from nats.js import JetStreamContext
from nats.js.api import PubAck

COMPRESSED = b"\x00agentihooks-state-zlib:"
PAGED = b"\x00agentihooks-state-pages:"
KV_HEADER_BUDGET = 1024


def validate(client: Client, subject: str, data: bytes, headers: dict[str, str] | None = None) -> None:
    overhead = len("NATS/1.0\r\n\r\n".encode()) if headers else 0
    overhead += sum(len(f"{key}: {value}\r\n".encode()) for key, value in (headers or {}).items())
    size = len(data) + overhead
    if size > client.max_payload:
        raise ValueError(f"{subject}: payload {size} exceeds server limit {client.max_payload}")


async def publish(js: JetStreamContext, subject: str, data: bytes, *, stream: str, headers: dict[str, str]) -> PubAck:
    validate(js._nc, subject, data, {**headers, "Nats-Expected-Stream": stream})
    return await js.publish(subject, data, stream=stream, headers=headers)


def scope(key: str, base: int) -> str:
    return f"{_owner(key)}.{base}"


def _owner(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()[:32]


async def encode(write: Callable[[str, bytes], Awaitable[None]], value: bytes, limit: int, scope: str) -> bytes:
    if limit < 128:
        raise ValueError("server payload budget is too small for state references")
    if len(value) <= limit and not value.startswith((COMPRESSED, PAGED)):
        return value
    value = COMPRESSED + zlib.compress(value)
    if len(value) <= limit:
        return value
    token = f"{scope}.{secrets.token_hex(16)}"
    count = (len(value) + limit - 1) // limit
    for index in range(count):
        key = f"_chunks.{token}.{index}"
        await write(key, value[index * limit : (index + 1) * limit])
    return PAGED + json.dumps([token, count]).encode()


async def decode(read: Callable[[str], Awaitable[bytes]], value: bytes) -> bytes:
    if value.startswith(PAGED):
        token, count = json.loads(value[len(PAGED) :])
        chunks = [await read(f"_chunks.{token}.{index}") for index in range(count)]
        value = b"".join(chunks)
    if value.startswith(COMPRESSED):
        return zlib.decompress(value[len(COMPRESSED) :])
    return value


async def reclaim(
    js: JetStreamContext,
    stream: str,
    prefix: str,
    key: str,
    history: Callable[[], Awaitable[list[bytes]]],
    below: int,
) -> None:
    pattern = f"{prefix}_chunks.{_owner(key)}."
    info = await js.stream_info(stream, subjects_filter=f"{pattern}>")
    stale: dict[str, list[str]] = {}
    for name in info.state.subjects or {}:
        base, nonce, _ = name[len(pattern) :].split(".")
        if int(base) < below:
            stale.setdefault(f"{_owner(key)}.{base}.{nonce}", []).append(name)
    if stale:
        for value in await history():
            if value.startswith(PAGED):
                stale.pop(json.loads(value[len(PAGED) :])[0], None)
    for names in stale.values():
        for name in names:
            await js.purge_stream(stream, subject=name)
