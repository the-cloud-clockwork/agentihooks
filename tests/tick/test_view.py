import io
import urllib.error
from collections.abc import Callable
from email.message import Message
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
    assert reader.calls == [(0, view.PARTS)]
    assert (held.generation, held.tasks, held.meta, held.phases) == (7, STATE["tasks"], STATE["_meta"], STATE["phases"])
    assert api.read_export_file(path) == (7, STATE)


@pytest.mark.feature("swarm-tick-delta-reads")
def test_a_later_pass_makes_one_delta_call_and_patches_the_stored_view(tmp_path: Path) -> None:
    path = tmp_path / "sw.json"
    api.write_export_file(path, 7, STATE)
    reader = Reader(changed(9))
    held = view.refreshed(path, reader)
    assert reader.calls == [(7, view.PARTS)]
    assert held.generation == 9
    assert held.meta == {"rev": 5, "events": []}
    assert api.read_export_file(path) == (9, {**STATE, "_meta": {"rev": 5, "events": []}})


@pytest.mark.feature("swarm-tick-delta-reads")
def test_an_exceeded_change_window_replaces_the_stored_view(tmp_path: Path) -> None:
    path = tmp_path / "sw.json"
    api.write_export_file(path, 7, {"tasks": [], "_meta": {}, "phases": []})
    reader = Reader({"generation": 900, "data": STATE})
    held = view.refreshed(path, reader)
    assert reader.calls == [(7, view.PARTS)]
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
def test_a_full_read_at_the_stored_generation_replaces_a_view_that_does_not_fit(tmp_path: Path) -> None:
    path = tmp_path / "sw.json"
    api.write_export_file(path, 7, {"tasks": []})
    reader = Reader(changed(7), {"generation": 7, "data": STATE})
    held = view.refreshed(path, reader)
    assert reader.calls == [(7, view.PARTS), (0, view.PARTS)]
    assert (held.generation, held.tasks) == (7, STATE["tasks"])
    assert api.read_export_file(path) == (7, STATE)


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


def test_only_the_ledger_client_itself_reads_the_view(monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.swarm import ledger_client

    class StandIn(ledger_client.LedgerClient):
        """A ledger client subclass, as the test suites stand in for the ledger."""

    monkeypatch.setattr(ledger_client, "LedgerClient", StandIn)
    assert view.reads(view.LedgerClient()) is True
    assert view.reads(StandIn()) is False
    assert view.reads(object()) is False


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
    assert held is not None
    assert sent == [("http://ledger/sw", {"X-Ledger-Token": "sw True"}, 4, "sw", 0, view.PARTS)]
    assert view.CURRENT.get() is held
    assert held.generation == 11
    assert api.read_export_file(view.view_file("http://ledger/sw", "sw", tmp_path)) == (11, STATE)


def refusing(code: int) -> Callable[..., dict]:
    def changes(self: api.ResourceClient, slug: str, since: int, parts: tuple[str, ...]) -> dict:
        raise urllib.error.HTTPError("http://ledger/export", code, "refused", Message(), io.BytesIO(b"{}"))

    return changes


def ledger_module() -> SimpleNamespace:
    return SimpleNamespace(base=lambda slug: "http://ledger", credentials=lambda slug, service: {}, REQUEST_TIMEOUT=4)


def test_a_server_that_predates_parts_leaves_the_pass_without_a_view(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from scripts.scheduler import service
    from scripts.swarm import ledger_client

    monkeypatch.setattr(ledger_client, "_ledger", ledger_module)
    monkeypatch.setattr(service, "runtime_dir", lambda: tmp_path)
    monkeypatch.setattr(api.ResourceClient, "changes", refusing(400))
    view.CURRENT.set(view.LedgerView(1, STATE))
    assert view.start("sw", False) is None
    assert view.CURRENT.get() is None
    assert not view.view_file("http://ledger", "sw", tmp_path).exists()
    assert capsys.readouterr().err == "ledger view of sw skipped: the ledger server refused named parts\n"


def test_any_other_refusal_fails_the_pass(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.scheduler import service
    from scripts.swarm import ledger_client

    monkeypatch.setattr(ledger_client, "_ledger", ledger_module)
    monkeypatch.setattr(service, "runtime_dir", lambda: tmp_path)
    monkeypatch.setattr(api.ResourceClient, "changes", refusing(503))
    with pytest.raises(urllib.error.HTTPError) as raised:
        view.start("sw", False)
    assert raised.value.code == 503


@pytest.mark.feature("swarm-tick-delta-reads")
def test_the_view_holds_every_top_level_part_the_pass_reads(tmp_path: Path) -> None:
    assert view.PARTS == ("tasks", "_meta", "phases", "freezes", "priorities", "followups", "alerts", "overview")
    state = {
        **STATE,
        "freezes": [{"verb": "focus", "target": "p1"}],
        "priorities": [{"item": "tasks/t1"}],
        "followups": [{"id": "f1", "text": "Later"}],
        "alerts": [{"id": "a1", "state": "open"}],
        "overview": "Intent",
    }
    held = view.refreshed(tmp_path / "sw.json", Reader({"generation": 3, "data": state}))
    assert held.document == state
    assert (held.tasks, held.meta, held.phases) == (state["tasks"], state["_meta"], state["phases"])


class Whole:
    """A ledger that records each whole state read and answers it with its own document."""

    def __init__(self, doc: dict) -> None:
        self.doc, self.reads = doc, []

    def state(self, slug: str) -> dict:
        self.reads.append(slug)
        return self.doc


@pytest.mark.feature("swarm-tick-delta-reads")
def test_a_pass_holding_a_view_reads_each_document_through_one_delta_read() -> None:
    from scripts.swarm.ledger_client import pass_state

    refreshes = []

    def refresh() -> view.LedgerView:
        refreshes.append(len(refreshes))
        return view.LedgerView(7 + len(refreshes), {**STATE, "overview": f"read {len(refreshes)}"})

    ledger = Whole({"tasks": []})
    with view.holding():
        view.REFRESH.set(("sw", refresh))
        first, second = pass_state(ledger, "sw"), pass_state(ledger, "sw")
        held = view.CURRENT.get()
    assert (first["overview"], second["overview"]) == ("read 1", "read 2")
    assert refreshes == [0, 1]
    assert held is not None and held.generation == 9
    assert ledger.reads == []


def test_a_pass_reads_another_ledger_whole() -> None:
    from scripts.swarm.ledger_client import pass_state

    ledger = Whole({"tasks": [{"id": "peer"}]})
    with view.holding():
        view.REFRESH.set(("sw", lambda: pytest.fail("the view of sw refreshed for its peer")))
        assert pass_state(ledger, "peer") == {"tasks": [{"id": "peer"}]}
    assert ledger.reads == ["peer"]


def test_without_a_held_view_the_ledger_is_read_whole() -> None:
    from scripts.swarm.ledger_client import pass_state

    ledger = Whole({"tasks": []})
    assert view.document("sw") is None
    assert pass_state(ledger, "sw") == {"tasks": []}
    assert ledger.reads == ["sw"]


def test_holding_drops_the_view_the_block_started() -> None:
    outer = view.LedgerView(1, STATE)
    view.CURRENT.set(outer)
    with view.holding():
        assert (view.CURRENT.get(), view.REFRESH.get()) == (None, None)
        view.CURRENT.set(view.LedgerView(2, STATE))
        view.REFRESH.set(("sw", lambda: view.LedgerView(3, STATE)))
    assert (view.CURRENT.get(), view.REFRESH.get()) == (outer, None)
    view.CURRENT.set(None)
