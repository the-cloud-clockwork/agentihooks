import json

from .fragments import encoded
from .rows import encode

SCALARS = frozenset({str, int, float, bool, type(None)})


def thawed(value: object) -> object:
    """A fresh copy of value with the containers and keys a JSON round trip would give, sharing immutable scalars."""
    kind = type(value)
    if kind is list:
        return [thawed(item) for item in value]
    if kind is dict and all(type(key) is str for key in value):
        return {key: thawed(item) for key, item in value.items()}
    if kind in SCALARS:
        return value
    return json.loads(encode(value))


class Entry:
    """One ledger as last stored: its generation, canonical text, a parsed copy callers only read, views of it and
    the private copy the write that stored it left for the next write."""

    def __init__(self, generation, text, state, fragments=None, writer=None):
        self.generation, self._text, self.state, self.views = generation, text, state, {}
        self.fragments, self.writer = fragments, writer

    @property
    def text(self) -> str:
        return encoded(self.fragments) if self._text is None else self._text

    def mutable(self) -> dict:
        return thawed(self.state)

    def for_write(self) -> dict:
        """The writer copy the write that stored this entry left, once; otherwise a fresh copy sharing stamp records."""
        writer, self.writer = self.writer, None
        if writer is not None:
            return writer
        meta = {key: value.copy() if key == "stamps" else thawed(value) for key, value in self.state["_meta"].items()}
        return {key: meta if key == "_meta" else thawed(value) for key, value in self.state.items()}
