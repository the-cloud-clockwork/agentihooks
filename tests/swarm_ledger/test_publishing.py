from scripts.swarm_ledger.events.publishing import Publishing


class Store:
    def __init__(self):
        self.calls = []

    def get_document(self, slug):
        self.calls.append(("get", slug))
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
    assert repository.get_document("s") == {"_meta": {"rev": 1}, "read": "s"}
    assert store.calls == [("get", "s")]
    assert published == [("s", {"_meta": {"rev": 1}, "read": "s"})]


def test_a_write_returns_state_and_refusals_and_publishes_the_state():
    store, published, repository = wrapped()
    assert repository.apply_ops("s", changes=["c"], ops=["o"], gate="g") == ({"_meta": {"rev": 2}}, ["refused"])
    assert store.calls == [("apply", "s", ["c"], ["o"], "g")]
    assert published == [("s", {"_meta": {"rev": 2}})]


def test_every_other_repository_call_passes_through_unpublished():
    _, published, repository = wrapped()
    assert repository.exists("here") and not repository.exists("gone")
    assert published == []


def test_module_copies_of_the_server_share_one_wrapper_per_repository():
    from scripts.swarm_ledger.events.publishing import publishing

    store, seen = Store(), []
    first = publishing(store, lambda slug, state: seen.append(("first", slug)))
    second = publishing(store, lambda slug, state: seen.append(("second", slug)))
    assert first is second
    publishing(store, first.publishers[0])
    assert len(first.publishers) == 2
    first.get_document("s")
    assert seen == [("first", "s"), ("second", "s")]
    assert publishing(Store(), lambda slug, state: None) is not first
