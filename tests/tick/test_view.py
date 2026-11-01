from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.swarm_ledger.api import client as api
from scripts.tick import view

STATE = {
    "tasks": [{"id": "t1", "state": "open"}, {"id": "t2", "state": "claimed"}],
    "_meta": {"rev": 4, "events": []},
    "phases": [{"id": "p1", "done": False}],
}


class Reader:
    """Answer each export read from a scripted list and record its since and parts."""

    def __init__(self, *replies: dict) -> None:
        self.replies = list(replies)
        self.calls: list[tuple[int, tuple[str, ...]]] = []

    def __call__(self, since: int, parts: tuple[str, ...]) -> dict:
        self.calls.append((since, parts))
        return self.replies.pop(0)


def changed(generation: int) -> dict:
    return {
        "generation": generation,
        "changes": {"set": [[["_meta", "rev"], 5]], "drop": [], "lists": {}},
    }


@pytest.mark.feature("swarm-tick-delta-reads")
def test_a_first_pass_reads_the_parts_in_full_and_stores_them(tmp_path: Path) -> None:
    path = tmp_path / "views" / "sw.json"
    reader = Reader({"generation": 7, "data": STATE})
    held = view.refreshed(path, reader)
    assert reader.calls == [(0, ("tasks", "_meta", "phases"))]
    assert (held.generation, held.tasks, held.meta, held.phases) == (7, STATE["tasks"], STATE["_meta"], STATE["phases"])
    assert api.read_export_file(path) == (7, STATE)


@pytest.mark.feature("swarm-tick-delta-reads")
def test_a_later_pass_makes_one_delta_call_and_patches_the_stored_view(tmp_path: Path) -> None:
    path = tmp_path / "sw.json"
    api.write_export_file(path, 7, STATE)
    reader = Reader(changed(9))
    held = view.refreshed(path, reader)
    assert reader.calls == [(7, ("tasks", "_meta", "phases"))]
    assert held.generation == 9
    assert held.meta == {"rev": 5, "events": []}
    assert api.read_export_file(path) == (9, {**STATE, "_meta": {"rev": 5, "events": []}})


@pytest.mark.feature("swarm-tick-delta-reads")
def test_an_exceeded_change_window_replaces_the_stored_view(tmp_path: Path) -> None:
    path = tmp_path / "sw.json"
    api.write_export_file(path, 7, {"tasks": [], "_meta": {}, "phases": []})
    reader = Reader({"generation": 900, "data": STATE})
    held = view.refreshed(path, reader)
    assert reader.calls == [(7, ("tasks", "_meta", "phases"))]
    assert (held.generation, held.tasks) == (900, STATE["tasks"])
    assert api.read_export_file(path) == (900, STATE)


@pytest.mark.feature("swarm-tick-delta-reads")
def test_a_patch_that_does_not_fit_falls_back_to_one_full_read(tmp_path: Path) -> None:
    path = tmp_path / "sw.json"
    api.write_export_file(path, 7, {"tasks": []})
    reader = Reader(changed(9), {"generation": 9, "data": STATE})
    held = view.refreshed(path, reader)
    assert reader.calls == [(7, view.PARTS), (0, view.PARTS)]
    assert (held.generation, held.phases) == (9, STATE["phases"])
    assert api.read_export_file(path) == (9, STATE)


@pytest.mark.feature("swarm-tick-delta-reads")
def test_an_unchanged_generation_leaves_the_stored_file(tmp_path: Path) -> None:
    path = tmp_path / "sw.json"
    api.write_export_file(path, 7, STATE)
    before = path.stat().st_ino
    held = view.refreshed(path, Reader({"generation": 7, "changes": {"set": [], "drop": [], "lists": {}}}))
    assert (held.generation, held.tasks) == (7, STATE["tasks"])
    assert path.stat().st_ino == before


def test_the_view_reads_rows_by_domain_and_key() -> None:
    held = view.LedgerView(3, STATE)
    assert held.read("tasks", "t2") == {"id": "t2", "state": "claimed"}
    assert held.read("phases", "p1") == {"id": "p1", "done": False}
    assert held.read("_meta", "rev") == 4
    assert held.read("tasks", "t9") is None
    assert held.read("chat", "t1") is None


def test_an_empty_view_holds_empty_parts() -> None:
    held = view.LedgerView(0, {})
    assert (held.tasks, held.meta, held.phases) == ([], {}, [])


def test_each_server_and_swarm_keeps_its_own_view_file(tmp_path: Path) -> None:
    names = {view.view_file(base, slug, tmp_path) for base in ("http://a", "http://b") for slug in ("s", "t")}
    assert len(names) == 4
    assert {name.parent for name in names} == {tmp_path / "ledger-views"}
    assert all(name.suffix == ".json" and len(name.stem) == 32 for name in names)


@pytest.mark.feature("swarm-tick-delta-reads")
def test_start_reads_through_the_ledger_client_and_holds_the_view(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.scheduler import service
    from scripts.swarm import ledger_client

    sent = []
    ledger = SimpleNamespace(
        base=lambda slug: f"http://ledger/{slug}",
        credentials=lambda slug, service: {"X-Ledger-Token": f"{slug} {service}"},
        REQUEST_TIMEOUT=4,
    )

    def changes(self: api.ResourceClient, slug: str, since: int, parts: tuple[str, ...]) -> dict:
        sent.append((self.base, self.credentials, self.timeout, slug, since, parts))
        return {"generation": 11, "data": STATE}

    monkeypatch.setattr(ledger_client, "_ledger", lambda: ledger)
    monkeypatch.setattr(service, "runtime_dir", lambda: tmp_path)
    monkeypatch.setattr(api.ResourceClient, "changes", changes)
    held = view.start("sw", service=True)
    assert sent == [("http://ledger/sw", {"X-Ledger-Token": "sw True"}, 4, "sw", 0, view.PARTS)]
    assert view.CURRENT.get() is held
    assert held.generation == 11
    assert api.read_export_file(view.view_file("http://ledger/sw", "sw", tmp_path)) == (11, STATE)
