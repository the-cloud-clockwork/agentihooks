import socket
import time

CHUNK = 1 << 16


class Body:
    def __init__(self, raw, length: int | None) -> None:
        self.raw, self.left = raw, length

    def read(self, size: int | None = -1) -> bytes:
        if self.left is None:
            return self.raw.read(size)
        size = self.left if size is None or size < 0 else min(size, self.left)
        data = self.raw.read(size) if size else b""
        self.left -= len(data)
        return data

    def drain(self, limit: int, connection, seconds: float, clock=time.monotonic) -> bool:
        if self.left is None or self.left > limit:
            return False
        deadline = clock() + seconds
        try:
            while self.left:
                remaining = deadline - clock()
                if remaining <= 0:
                    return False
                connection.settimeout(remaining)
                data = self.raw.read1(min(CHUNK, self.left))
                if not data:
                    return False
                self.left -= len(data)
        except OSError:
            return False
        finally:
            connection.settimeout(None)
        return True

    def close(self) -> None:
        self.raw.close()


def length(headers) -> int | None:
    if headers.get("Transfer-Encoding"):
        return None
    try:
        found = int(headers.get("Content-Length") or 0)
    except ValueError:
        return None
    return found if found >= 0 else None


def linger(connection, seconds: float, clock=time.monotonic) -> None:
    deadline = clock() + seconds
    try:
        connection.shutdown(socket.SHUT_WR)
        while (remaining := deadline - clock()) > 0:
            connection.settimeout(remaining)
            if not connection.recv(CHUNK):
                return
    except OSError:
        return
