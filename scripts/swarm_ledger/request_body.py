CHUNK = 1 << 16


class Body:
    def __init__(self, raw, length: int) -> None:
        self.raw, self.left = raw, length

    def read(self, size: int | None = -1) -> bytes:
        size = self.left if size is None or size < 0 else min(size, self.left)
        data = self.raw.read(size) if size else b""
        self.left -= len(data)
        return data

    def drain(self, limit: int) -> bool:
        if self.left > limit:
            return False
        while self.left:
            if not self.read(CHUNK):
                return False
        return True

    def __getattr__(self, name: str):
        return getattr(self.raw, name)


def length(headers) -> int:
    try:
        return max(int(headers.get("Content-Length") or 0), 0)
    except ValueError:
        return 0
