class Publishing:
    """A LedgerRepository that hands every ledger it reads or writes to each publisher(slug, state)."""

    def __init__(self, inner, *publishers):
        self.inner = inner
        self.publishers = list(publishers)

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def publish(self, slug, state):
        for publisher in self.publishers:
            publisher(slug, state)

    def get_document(self, slug: str) -> dict:
        state = self.inner.get_document(slug)
        self.publish(slug, state)
        return state

    def apply_ops(
        self, slug: str, changes: list | None = None, ops: list | None = None, gate=None
    ) -> tuple[dict, list]:
        state, rejected = self.inner.apply_ops(slug, changes=changes, ops=ops, gate=gate)
        self.publish(slug, state)
        return state, rejected


SHARED = {}


def publishing(inner, publisher):
    """The one wrapper of a repository, shared by every module copy of the server that adds its publisher."""
    wrapper = SHARED.setdefault(id(inner), Publishing(inner))
    if publisher not in wrapper.publishers:
        wrapper.publishers.append(publisher)
    return wrapper
