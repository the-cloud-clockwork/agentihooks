import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "swarm_ledger"))

from scripts.swarm_ledger import ledger_server as server  # noqa: E402
from scripts.swarm_ledger.events import Hub  # noqa: E402

LEDGER = {
    "tasks": [{"id": "t1", "workspace": "w"}, {"id": "t2"}, {"id": "bad id", "workspace": "w"}],
    "_meta": {"rev": 3},
}


from scripts.swarm import command_runner, commands
from scripts.swarm_ledger import ledger_workspace


@pytest.fixture
def folders(tmp_path, monkeypatch):
    real = ledger_workspace.folder
    monkeypatch.setattr(ledger_workspace, "folder", lambda slug, task: (real(slug, task), tmp_path / task)[1])
    monkeypatch.setattr(server, "swarm_store", lambda: "store")
    monkeypatch.setattr(server, "HUB", Hub())
    published = {}
    monkeypatch.setattr(commands, "publish", lambda store, slug, status, tails: published.update({slug: tails}))
    monkeypatch.setattr(commands, "workspaces", lambda store, slug: published.get(slug, {}))
    monkeypatch.setattr(command_runner, "status_report", lambda *args: {})
    (tmp_path / "t1").mkdir()
    return tmp_path


def counted(monkeypatch, module, name):
    calls = []
    real = getattr(module, name)
    monkeypatch.setattr(module, name, lambda *a, **kw: (calls.append(a), real(*a, **kw))[1])
    return calls


def test_a_work_folder_read_returns_the_lines_the_tick_last_published(folders, monkeypatch):
    progress = folders / "t1" / "progress.md"
    assert server.workspace_tails("s", "t1") == {}
    progress.write_text("first step\n")
    assert server.workspace_tails("s", "t1") == {}
    command_runner.publish("store", "s", {"tasks": [LEDGER["tasks"][0]]})
    assert server.workspace_tails("s", "t1") == {"latest_progress": "first step"}
    progress.write_text("first step\nsecond step\n")
    (folders / "t1" / "proof.md").write_text("run green\n")
    command_runner.publish("store", "s", {"tasks": [LEDGER["tasks"][0]]})
    assert server.workspace_tails("s", "t1") == {
        "latest_progress": "first step\nsecond step",
        "latest_proof": "run green",
    }
    assert server.workspace_tails("s", "t2") == {}


def test_a_work_folder_read_refuses_an_unsafe_task_id(folders):
    with pytest.raises(ValueError, match="bad id"):
        server.workspace_tails("s", "bad id")


def test_a_work_folder_read_without_a_swarm_answers_no_lines(monkeypatch):
    from scripts.swarm.store import SwarmError

    def missing(store, slug):
        raise SwarmError("no swarm")

    monkeypatch.setattr(server, "swarm_store", lambda: "store")
    monkeypatch.setattr(commands, "workspaces", missing)
    assert server.workspace_tails("s", "t1") == {}


def test_one_ledger_failing_its_swarm_read_never_stops_the_others(folders, monkeypatch, capsys):
    def status(slug, state=None):
        if slug == "a":
            raise RuntimeError("redis went away")
        return {"config": {"state": state["_meta"]["rev"]}}

    monkeypatch.setattr(server, "swarm_status", status)
    for slug in ("a", "b"):
        server.HUB.open(slug, lambda: {"ledger": LEDGER, "swarm": None})
    server.sample_streams()
    assert "stream sample a: redis went away" in capsys.readouterr().err
    assert server.HUB.resource("b", "swarm") == {"config": {"state": 3}}
    assert server.HUB.resource("a", "swarm") is None


def test_idle_sampling_reads_no_ledger_document_and_sends_nothing(folders, monkeypatch):
    monkeypatch.setattr(server, "swarm_status", lambda slug, state=None: {"config": {"state": "running"}})
    server.HUB.open("s", lambda: {"ledger": LEDGER, "swarm": {"config": {"state": "running"}}})
    snapshots = counted(monkeypatch, server.repository, "read")
    documents = counted(monkeypatch, server.repository, "get_document")
    versions = counted(monkeypatch, server.core, "page_version")
    tails = counted(monkeypatch, ledger_workspace, "tails")
    for _ in range(5):
        server.sample_streams()
    assert (snapshots, documents, versions, tails) == ([], [], [], [])
    assert server.HUB.channels["s"].seq == 0


def test_sampling_skips_a_watched_ledger_without_a_loaded_copy(folders, monkeypatch):
    monkeypatch.setattr(server, "swarm_status", lambda slug, state=None: pytest.fail("sampled"))
    server.HUB.open("s", lambda: {"swarm": None})
    server.sample_streams()


def test_the_served_page_version_is_computed_once_per_process(monkeypatch):
    calls = []
    monkeypatch.setattr(server.core, "page_version", lambda assets: calls.append(1) or "v1")
    server.served_page.cache_clear()
    try:
        assert server.ledger_view({"tasks": [], "_meta": {"rev": 1, "seeds": {}, "api_operations": {}}})["_meta"] == {
            "rev": 1,
            "page_version": "v1",
            "crew": [],
        }
        server.ledger_view({"_meta": {"rev": 2}})
        assert calls == [1]
    finally:
        server.served_page.cache_clear()


def test_a_published_view_is_a_copy_the_caller_cannot_change(monkeypatch):
    monkeypatch.setattr(server, "HUB", Hub())
    server.HUB.open("s", lambda: {"ledger": {"tasks": [], "_meta": {"rev": 0}}})
    state = {"tasks": [{"id": "t1"}], "_meta": {"rev": 1, "seeds": {"0": {}}}}
    server.publish_ledger("s", state)
    state["tasks"][0]["id"] = "changed"
    assert server.HUB.resource("s", "ledger")["tasks"] == [{"id": "t1"}]
    server.publish_ledger("unwatched", state)
    assert not server.HUB.has("unwatched")


@pytest.mark.parametrize("newest", ["a.py", "b.html", "c.js", "d.css"])
def test_the_code_stamp_follows_every_page_asset_kind(tmp_path, newest):
    for name in ("a.py", "b.html", "c.js", "d.css", "e.md"):
        (tmp_path / name).write_text("x")
        os.utime(tmp_path / name, ns=(1, 90 if name == "e.md" else 50 if name == newest else 10))
    assert server.code_stamp([tmp_path]) == 50


def test_the_code_stamp_of_a_folder_without_code_is_zero(tmp_path):
    (tmp_path / "notes.md").write_text("x")
    assert server.code_stamp([tmp_path]) == 0


def test_stream_resources_loads_the_stored_view_without_reconciling(monkeypatch):
    calls = []
    state = {"tasks": [], "_meta": {"rev": 7}}

    def get_document(slug):
        calls.append(("document", slug))
        return state

    monkeypatch.setattr(server.repository, "get_document", get_document)
    monkeypatch.setattr(server, "ledger_view", lambda given: {**given, "view": True})
    monkeypatch.setattr(server, "swarm_status", lambda slug, ledger: calls.append(("swarm", slug, ledger)) or "S")
    monkeypatch.setattr(server, "workspace_tails", lambda *a: calls.append(("tails", *a)))
    view = {**state, "view": True}
    assert server.stream_resources("s") == {"ledger": view, "swarm": "S"}
    assert calls == [("document", "s"), ("swarm", "s", view)]


def test_sampling_leaves_work_folder_changes_to_the_lazy_read(folders, monkeypatch):
    monkeypatch.setattr(server, "swarm_status", lambda slug, state=None: None)
    server.HUB.open("empty", lambda: {"swarm": None})
    server.HUB.open("s", lambda: {"ledger": LEDGER, "swarm": None})
    (folders / "t1" / "progress.md").write_text("step one\n")
    command_runner.publish("store", "s", {"tasks": [LEDGER["tasks"][0]]})
    tails = counted(monkeypatch, ledger_workspace, "tails")
    reads = counted(monkeypatch, commands, "workspaces")
    server.sample_streams()
    assert reads == []
    assert tails == []
    assert server.HUB.resource("s", "workspaces") is None
    assert list(server.HUB.channels["s"].log) == []


def test_swarm_status_reads_the_snapshot_only_without_a_given_state(monkeypatch):
    reads = []
    monkeypatch.setattr(server, "swarm_store", lambda: "store")
    monkeypatch.setattr(commands, "view", lambda store, slug: {"quota": "published"})
    monkeypatch.setattr(server.repository, "read", lambda slug: reads.append(slug))
    assert server.swarm_status("s", {"given": 1}) == {"quota": "published"}
    assert server.swarm_status("s") == {"quota": "published"}
    assert reads == []
