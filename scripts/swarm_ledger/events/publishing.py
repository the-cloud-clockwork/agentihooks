class Publishing:
    """A LedgerRepository that hands every ledger it reads or writes to publish(slug, state)."""

    def __init__(self, inner, publish):
        self.inner = inner
        self.publish = publish

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def get_document(self, slug: str, reconcile: bool = True) -> dict:
        state = self.inner.get_document(slug, reconcile)
        self.publish(slug, state)
        return state

    def apply_ops(
        self, slug: str, changes: list | None = None, ops: list | None = None, gate=None
    ) -> tuple[dict, list]:
        state, rejected = self.inner.apply_ops(slug, changes=changes, ops=ops, gate=gate)
        self.publish(slug, state)
        return state, rejected
