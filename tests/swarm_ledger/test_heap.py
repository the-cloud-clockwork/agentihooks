import ctypes
import importlib

import pytest

from scripts.swarm_ledger.repository import heap


def test_release_asks_the_c_library_to_trim_every_free_page(monkeypatch):
    calls = []
    monkeypatch.setattr(heap, "TRIM", calls.append)
    heap.release()
    assert calls == [0]


def test_release_is_a_no_op_where_the_c_library_has_no_trim(monkeypatch):
    monkeypatch.setattr(heap, "TRIM", None)
    heap.release()


def test_a_c_library_without_trim_resolves_to_none(monkeypatch):
    monkeypatch.setattr(ctypes, "CDLL", lambda name: object())
    try:
        assert importlib.reload(heap).TRIM is None
    finally:
        monkeypatch.undo()
        importlib.reload(heap)


@pytest.mark.skipif(heap.TRIM is None, reason="malloc_trim is glibc only")
def test_glibc_hosts_resolve_the_trim_from_the_running_process():
    assert callable(heap.TRIM)
