CHUNK = 1 << 16


class Body:
    def __init__(self, raw, length: int | None) -> None:
        self.raw, self.left = raw, length

    def read(self, size: int | None = -1) -> bytes:
        left = self.left or 0
        size = left if size is None or size < 0 else min(size, left)
        data = self.raw.read(size) if size else b""
        if self.left is not None:
            self.left -= len(data)
        return data

    def drain(self, limit: int) -> bool:
        if self.left is None or self.left > limit:
            return False
        while self.left:
            if not self.read(CHUNK):
                return False
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
