import hashlib
import json
import zlib

from nats.aio.client import Client
from nats.js import JetStreamContext
from nats.js.api import PubAck
from nats.js.errors import KeyWrongLastSequenceError
from nats.js.kv import KeyValue

COMPRESSED = b"\x00agentihooks-state-zlib:"
PAGED = b"\x00agentihooks-state-pages:"
KV_HEADER_BUDGET = 1024


def validate(client: Client, subject: str, data: bytes, headers: dict[str, str] | None = None) -> None:
    overhead = len("NATS/1.0\r\n\r\n".encode()) if headers else 0
    overhead += sum(len(f"{key}: {value}\r\n".encode()) for key, value in (headers or {}).items())
    size = len(data) + overhead
    if size > client.max_payload:
        raise ValueError(f"{subject}: payload {size} exceeds server limit {client.max_payload}")


async def publish(
    js: JetStreamContext, subject: str, data: bytes, *, stream: str, headers: dict[str, str]
) -> PubAck:
    headers = {**headers, "Nats-Expected-Stream": stream}
    validate(js._nc, subject, data, headers)
    return await js.publish(subject, data, stream=stream, headers=headers)


async def encode(kv: KeyValue, value: bytes, limit: int) -> bytes:
    if limit < 128:
        raise ValueError("server payload budget is too small for state references")
    if len(value) <= limit and not value.startswith((COMPRESSED, PAGED)):
        return value
    value = COMPRESSED + zlib.compress(value)
    if len(value) <= limit:
        return value
    token = hashlib.sha256(str(limit).encode() + b":" + value).hexdigest()
    count = (len(value) + limit - 1) // limit
    for index in range(count):
        key = f"_chunks.{token}.{index}"
        try:
            await kv.create(key, value[index * limit : (index + 1) * limit])
        except KeyWrongLastSequenceError:
            continue
    return PAGED + json.dumps([token, count]).encode()


async def decode(kv: KeyValue, value: bytes) -> bytes:
    if value.startswith(PAGED):
        token, count = json.loads(value[len(PAGED) :])
        chunks = [await kv.get(f"_chunks.{token}.{index}") for index in range(count)]
        value = b"".join(chunk.value for chunk in chunks)
    if value.startswith(COMPRESSED):
        return zlib.decompress(value[len(COMPRESSED) :])
    return value
