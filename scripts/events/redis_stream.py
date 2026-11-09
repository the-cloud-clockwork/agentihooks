import base64
import io
import re
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any, BinaryIO

from redis.exceptions import ResponseError

CHUNK = 64 * 1024


class BulkReader:
    def __init__(self, source: BinaryIO, remaining: int) -> None:
        self.source = source
        self.remaining = remaining

    def read(self, size: int = -1) -> bytes:
        size = min(self.remaining, CHUNK, CHUNK if size < 0 else size)
        value = self.source.read(size)
        if size and not value:
            raise EOFError("Redis ended a bulk response early")
        self.remaining -= len(value)
        return value


@contextmanager
def field(redis: Any, key: str, name: str) -> Generator[BulkReader | None, None, None]:
    connection = redis.connection_pool.get_connection("HGET")
    source = None
    try:
        connection.send_command("HGET", key, name)
        source = connection._sock.makefile("rb")
        header = source.readline()
        if header.startswith(b"-"):
            raise ResponseError(header[1:].decode().strip())
        if not header.startswith(b"$"):
            raise ValueError("Redis HGET did not return a bulk response")
        length = int(header[1:])
        yield None if length == -1 else BulkReader(source, length)
    finally:
        if source is not None:
            source.close()
        connection.disconnect()
        redis.connection_pool.release(connection)


class Envelope(io.RawIOBase):
    def __init__(self, source: BulkReader) -> None:
        self.source = source
        header = bytearray()
        while not header.endswith(b'"value": "'):
            byte = source.read(1)
            if not byte or len(header) >= 256:
                raise ValueError("invalid Redis state envelope")
            header.extend(byte)
        match = re.search(rb'"revision":\s*(\d+)', header)
        if match is None:
            raise ValueError("Redis state envelope has no revision")
        self.revision = int(match[1])
        self.encoded = b""
        self.decoded = b""
        self.ended = False

    def readable(self) -> bool:
        return True

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            raise ValueError("state decoding requires bounded reads")
        size = min(size, CHUNK)
        while len(self.decoded) < size and not self.ended:
            chunk = self.source.read(CHUNK)
            if not chunk:
                raise EOFError("Redis state envelope ended before its value")
            if b'"' in chunk:
                chunk = chunk.split(b'"', 1)[0]
                self.ended = True
            self.encoded += chunk
            length = len(self.encoded) // 4 * 4
            self.decoded += base64.b64decode(self.encoded[:length], validate=True)
            self.encoded = self.encoded[length:]
        value, self.decoded = self.decoded[:size], self.decoded[size:]
        return value

    def readinto(self, buffer: Any) -> int:
        value = self.read(len(buffer))
        buffer[: len(value)] = value
        return len(value)
