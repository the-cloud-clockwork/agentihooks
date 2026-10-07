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


@pytest.fixture
def folders(tmp_path, monkeypatch):
    real = server.ledger_workspace.folder
    monkeypatch.setattr(server.ledger_workspace, "folder", lambda slug, task: (real(slug, task), tmp_path / task)[1])
    monkeypatch.setattr(server, "TAIL_MARKS", {})
    monkeypatch.setattr(server, "HUB", Hub())
    (tmp_path / "t1").mkdir()
    return tmp_path


def counted(monkeypatch, module, name):
    calls = []
    real = getattr(module, name)
    monkeypatch.setattr(module, name, lambda *a, **kw: (calls.append(a), real(*a, **kw))[1])
    return calls


def test_tails_are_read_again_only_when_a_work_file_changes(folders, monkeypatch):
    reads = counted(monkeypatch, server.ledger_workspace, "tails")
    progress = folders / "t1" / "progress.md"
    progress.write_text("first step\n")
    assert server.workspace_tails("s", LEDGER) == {"t1": {"latest_progress": "first step"}}
    assert server.workspace_tails("s", LEDGER) == {"t1": {"latest_progress": "first step"}}
    assert len(reads) == 1
    progress.write_text("first step\nsecond step\n")
    os.utime(progress, ns=(progress.stat().st_atime_ns, progress.stat().st_mtime_ns + 1_000_000))
    assert server.workspace_tails("s", LEDGER) == {"t1": {"latest_progress": "first step\nsecond step"}}
    assert len(reads) == 2
    (folders / "t1" / "proof.md").write_text("run green\n")
    assert server.workspace_tails("s", LEDGER)["t1"]["latest_proof"] == "run green"
    assert len(reads) == 3


def test_tails_skip_tasks_without_a_folder_or_with_an_unsafe_id(folders):
    assert server.workspace_tails("s", LEDGER) == {"t1": {}}
    assert list(server.TAIL_MARKS["s"]) == ["t1"]


def test_marks_follow_the_current_tasks_and_ledgers(folders):
    server.workspace_tails("s", LEDGER)
    server.workspace_tails("s", {"tasks": [], "_meta": {"rev": 4}})
    assert server.TAIL_MARKS["s"] == {}
    server.HUB.open("s", lambda: {"ledger": LEDGER})
    server.TAIL_MARKS["gone"] = {"t9": ((None, None), {})}
    server.sample_streams()
    assert set(server.TAIL_MARKS) == {"s"}


def test_tail_stamp_is_none_for_a_missing_file(tmp_path):
    (tmp_path / "f").write_text("x")
    assert server.tail_stamp(tmp_path / "f") == (tmp_path / "f").stat().st_mtime_ns
    assert server.tail_stamp(tmp_path / "missing") is None


def test_one_ledger_failing_its_swarm_read_never_stops_the_others(folders, monkeypatch, capsys):
    def status(slug, state=None):
        if slug == "a":
            raise RuntimeError("redis went away")
        return {"config": {"state": state["_meta"]["rev"]}}

    monkeypatch.setattr(server, "swarm_status", status)
    for slug in ("a", "b"):
        server.HUB.open(slug, lambda: {"ledger": LEDGER, "swarm": None, "workspaces": {}})
    server.sample_streams()
    assert "stream sample a: redis went away" in capsys.readouterr().err
    assert server.HUB.resource("b", "swarm") == {"config": {"state": 3}}
    assert server.HUB.resource("a", "swarm") is None


def test_idle_sampling_reads_no_ledger_document_and_sends_nothing(folders, monkeypatch):
    monkeypatch.setattr(server, "swarm_status", lambda slug, state=None: {"config": {"state": "running"}})
    server.HUB.open(
        "s", lambda: {"ledger": LEDGER, "swarm": {"config": {"state": "running"}}, "workspaces": {"t1": {}}}
    )
    snapshots = counted(monkeypatch, server.repository, "read_snapshot")
    documents = counted(monkeypatch, server.repository, "get_document")
    versions = counted(monkeypatch, server.core, "page_version")
    tails = counted(monkeypatch, server.ledger_workspace, "tails")
    server.workspace_tails("s", LEDGER)
    tails.clear()
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
    monkeypatch.setattr(server.core, "page_version", lambda: calls.append(1) or "v1")
    server.served_version.cache_clear()
    try:
        assert server.ledger_view({"tasks": [], "_meta": {"rev": 1, "seeds": {}, "api_operations": {}}})["_meta"] == {
            "rev": 1,
            "page_version": "v1",
            "crew": [],
        }
        server.ledger_view({"_meta": {"rev": 2}})
        assert calls == [1]
    finally:
        server.served_version.cache_clear()


def test_a_published_view_is_a_copy_the_caller_cannot_change(monkeypatch):
    monkeypatch.setattr(server, "HUB", Hub())
    server.HUB.open("s", lambda: {"ledger": {"tasks": [], "_meta": {"rev": 0}}})
    state = {"tasks": [{"id": "t1"}], "_meta": {"rev": 1, "seeds": {"0": {}}}}
    server.publish_ledger("s", state)
    state["tasks"][0]["id"] = "changed"
    assert server.HUB.resource("s", "ledger")["tasks"] == [{"id": "t1"}]
    server.publish_ledger("unwatched", state)
    assert not server.HUB.has("unwatched")


def test_the_code_stamp_follows_page_assets(tmp_path):
    for name in ("a.py", "b.html", "c.js", "d.css", "e.md"):
        (tmp_path / name).write_text("x")
        os.utime(tmp_path / name, ns=(1, {"a.py": 10, "b.html": 20, "c.js": 30, "d.css": 40, "e.md": 50}[name]))
    assert server.code_stamp([tmp_path]) == 40
