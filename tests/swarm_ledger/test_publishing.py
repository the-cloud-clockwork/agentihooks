import json
import threading
from types import SimpleNamespace

import pytest

from scripts.swarm_ledger.events.publishing import Publishing


class Store:
    def __init__(self):
        self.calls = []
        self.generation = 0

    def current(self, slug):
        self.calls.append(("current", slug))
        state = {"_meta": {"rev": self.generation + 1}, "read": slug}
        return SimpleNamespace(generation=self.generation, text=json.dumps(state), state=state)

    def apply_ops(self, slug, changes=None, ops=None, gate=None):
        self.calls.append(("apply", slug, changes, ops, gate))
        self.generation += 1
        return {"_meta": {"rev": 2}}, ["refused"]

    def exists(self, slug):
        return slug == "here"


def wrapped():
    store, published = Store(), []
    return store, published, Publishing(store, lambda slug, state: published.append((slug, state)))


def test_a_read_returns_the_stored_document_and_publishes_each_generation_once():
    store, published, repository = wrapped()
    assert repository.get_document("s") == {"_meta": {"rev": 1}, "read": "s"}
    repository.drain()
    assert repository.get_document("s") == {"_meta": {"rev": 1}, "read": "s"}
    repository.drain()
    assert store.calls == [("current", "s"), ("current", "s")]
    assert published == [("s", {"_meta": {"rev": 1}, "read": "s"})]
    assert repository.current("t").state == {"_meta": {"rev": 1}, "read": "t"}
    repository.drain()
    store.generation = 4
    repository.current("s")
    repository.drain()
    store.generation = 3
    repository.current("s")
    repository.drain()
    assert published == [
        ("s", {"_meta": {"rev": 1}, "read": "s"}),
        ("t", {"_meta": {"rev": 1}, "read": "t"}),
        ("s", {"_meta": {"rev": 5}, "read": "s"}),
    ]


def test_publication_runs_on_one_daemon_thread_apart_from_the_caller():
    threads = []
    repository = Publishing(Store(), lambda slug, state: threads.append(threading.current_thread()))
    repository.current("s")
    repository.current("t")
    repository.drain()
    assert threads == [repository.worker] * 2
    assert repository.worker is not threading.current_thread()
    assert repository.worker.daemon
    assert repository.worker.name == "ledger-publish"


def test_a_generation_whose_publication_failed_is_published_at_the_next_read(capsys):
    seen = []

    def flaky(slug, state):
        seen.append(state["_meta"]["rev"])
        if len(seen) == 1:
            raise OSError("stream gone")

    repository = Publishing(Store(), flaky)
    repository.current("s")
    repository.drain()
    assert "stream publish for s: stream gone" in capsys.readouterr().err
    repository.current("s")
    repository.drain()
    repository.current("s")
    repository.drain()
    assert seen == [1, 1]


def test_a_read_of_a_published_generation_queues_nothing():
    _, published, repository = wrapped()
    repository.current("s")
    repository.drain()
    repository.queued = None
    repository.current("s")
    assert published == [("s", {"_meta": {"rev": 1}, "read": "s"})]


def held(repository, published):
    started, release = threading.Event(), threading.Event()

    def blocking(slug, state):
        started.set()
        assert release.wait(10)
        published.append((slug, state["_meta"]["rev"]))

    repository.publishers = [blocking]
    return started, release


def test_only_the_newest_queued_generation_of_a_ledger_is_published_and_only_once():
    store, published, repository = wrapped()
    started, release = held(repository, published)
    repository.current("busy")
    assert started.wait(10)
    store.generation = 2
    repository.current("s")
    store.generation = 1
    repository.current("s")
    repository.current("s")
    assert repository.pending["s"].generation == 2
    release.set()
    repository.drain()
    assert published == [("busy", 1), ("s", 3)]


def test_drain_waits_for_a_publication_already_running():
    _, published, repository = wrapped()
    started, release = held(repository, published)
    repository.current("s")
    assert started.wait(10)
    draining = threading.Thread(target=repository.drain)
    draining.start()
    draining.join(0.2)
    assert draining.is_alive() and not repository.pending
    release.set()
    draining.join(10)
    assert not draining.is_alive()
    assert published == [("s", 1)]


def test_a_write_returns_state_and_refusals_and_publishes_the_stored_generation():
    store, published, repository = wrapped()
    assert repository.apply_ops("s", changes=["c"], ops=["o"], gate="g") == ({"_meta": {"rev": 2}}, ["refused"])
    repository.drain()
    assert store.calls == [("apply", "s", ["c"], ["o"], "g"), ("current", "s")]
    assert published == [("s", {"_meta": {"rev": 2}, "read": "s"})]


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
    first.drain()
    assert seen == [("first", "s"), ("second", "s")]
    assert publishing(Store(), lambda slug, state: None) is not first
