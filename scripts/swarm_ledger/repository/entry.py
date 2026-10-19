from .fragments import encoded


class Entry:
    """One ledger as last stored: its generation, canonical text, a parsed copy callers only read and views of it."""

    def __init__(self, generation, text, state, fragments=None):
        self.generation, self._text, self.state, self.views = generation, text, state, {}
        self.fragments = fragments

    @property
    def text(self) -> str:
        return encoded(self.fragments) if self._text is None else self._text
