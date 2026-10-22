from scripts.swarm_ledger.repository import heap


def test_release_asks_the_c_library_to_trim_every_free_page(monkeypatch):
    calls = []
    monkeypatch.setattr(heap, "TRIM", lambda pad: calls.append(pad) or 1)
    assert heap.release() is True
    assert calls == [0]


def test_release_reports_when_nothing_was_handed_back(monkeypatch):
    monkeypatch.setattr(heap, "TRIM", lambda pad: 0)
    assert heap.release() is False


def test_release_is_a_no_op_where_the_c_library_has_no_trim(monkeypatch):
    monkeypatch.setattr(heap, "TRIM", None)
    assert heap.release() is False


def test_glibc_hosts_resolve_the_trim_from_the_running_process():
    assert callable(heap.TRIM)
