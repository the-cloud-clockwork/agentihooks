from scripts.swarm_ledger.events.publishing import Publishing


class Store:
    def __init__(self):
        self.calls = []

    def get_document(self, slug, reconcile=True):
        self.calls.append(("get", slug, reconcile))
        return {"_meta": {"rev": 1}, "read": slug}

    def apply_ops(self, slug, changes=None, ops=None, gate=None):
        self.calls.append(("apply", slug, changes, ops, gate))
        return {"_meta": {"rev": 2}}, ["refused"]

    def exists(self, slug):
        return slug == "here"


def wrapped():
    store, published = Store(), []
    return store, published, Publishing(store, lambda slug, state: published.append((slug, state)))


def test_a_read_returns_the_stored_document_and_publishes_it():
    store, published, repository = wrapped()
    assert repository.get_document("s", reconcile=False) == {"_meta": {"rev": 1}, "read": "s"}
    assert store.calls == [("get", "s", False)]
    assert published == [("s", {"_meta": {"rev": 1}, "read": "s"})]
    repository.get_document("t")
    assert store.calls[-1] == ("get", "t", True)


def test_a_write_returns_state_and_refusals_and_publishes_the_state():
    store, published, repository = wrapped()
    assert repository.apply_ops("s", changes=["c"], ops=["o"], gate="g") == ({"_meta": {"rev": 2}}, ["refused"])
    assert store.calls == [("apply", "s", ["c"], ["o"], "g")]
    assert published == [("s", {"_meta": {"rev": 2}})]


def test_every_other_repository_call_passes_through_unpublished():
    _, published, repository = wrapped()
    assert repository.exists("here") and not repository.exists("gone")
    assert published == []
