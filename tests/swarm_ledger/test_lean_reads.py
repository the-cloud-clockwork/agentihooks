import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import ledger
import ledger_core as core
import ledger_server
import ledger_workspace
import new_ledger
import pytest

SLUG = "lean-reads-2026-01-01"


def make_ledger():
    doc = new_ledger.build_doc(
        {
            "title": "Lean",
            "overview": "o",
            "sources": [],
            "phases": [{"title": "one", "description": "d"}],
            "followups": [{"text": "check disk"}],
        }
    )
    html_path, json_path = core.paths(SLUG)
    html_path.write_text(new_ledger.render(doc, SLUG, 8765), encoding="utf-8")
    json_path.unlink(missing_ok=True)
    return core.sync(SLUG)[0]


def say(n):
    return core.sync(SLUG, ops=[{"op": "add", "thread": "chat", "id": f"m{n}", "text": f"note {n}"}])[0]


def test_a_read_that_changes_nothing_writes_nothing():
    make_ledger()
    say(1)
    html_path, json_path = core.paths(SLUG)
    before = (json_path.stat().st_mtime_ns, html_path.stat().st_mtime_ns)
    for _ in range(10):
        core.sync(SLUG)
    assert (json_path.stat().st_mtime_ns, html_path.stat().st_mtime_ns) == before


def test_storage_keeps_only_the_last_few_seeds_and_every_entry():
    make_ledger()
    for n in range(core.SEEDS_KEPT + 10):
        say(n)
    stored = json.loads(core.paths(SLUG)[1].read_text(encoding="utf-8"))
    assert len(stored["_meta"]["seeds"]) <= 5
    assert [m["text"] for m in stored["chat"]] == [f"note {n}" for n in range(core.SEEDS_KEPT + 10)]


def test_an_html_seed_older_than_the_kept_seeds_reverts_nothing():
    state = make_ledger()
    html_path = core.paths(SLUG)[0]
    stale = html_path.read_text(encoding="utf-8")
    item = state["followups"][0]["id"]
    core.sync(SLUG, changes=[{"path": f"followups/{item}/done", "value": True}])
    for n in range(core.SEEDS_KEPT + 2):
        say(n)
    html_path.write_text(stale, encoding="utf-8")
    state, _ = core.sync(SLUG)
    assert state["followups"][0]["done"] is True
    assert len(state["chat"]) == core.SEEDS_KEPT + 2
    assert any("too old" in w for w in state["_meta"]["warnings"])


@pytest.fixture
def served(monkeypatch):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), ledger_server.Handler)
    base = f"http://127.0.0.1:{httpd.server_port}"
    monkeypatch.setattr(ledger_server, "ALLOWED_HOSTS", {f"127.0.0.1:{httpd.server_port}"})
    monkeypatch.setattr(ledger, "BASE", base)
    threading.Thread(target=httpd.serve_forever, args=(0.01,), daemon=True).start()
    yield base
    httpd.shutdown()
    httpd.server_close()


def with_work_folder():
    make_ledger()
    folder = ledger_workspace.scaffold(SLUG, {"id": "t1", "title": "a"})
    (folder / "progress.md").write_text("red test seen\n", encoding="utf-8")
    op = {"op": "task_add", "id": "ta1", "by": "boss", "task": "t1", "title": "a", "lane": "eng"}
    core.sync(SLUG, ops=[{**op, "workspace": str(folder)}])


def test_the_page_read_carries_work_folder_tails_but_no_seed_copies(served):
    with_work_folder()
    token = core.read_token(core.paths(SLUG)[0].read_text(encoding="utf-8"))
    request = urllib.request.Request(f"{served}/api/{SLUG}", headers={"X-Ledger-Token": token})
    with urllib.request.urlopen(request) as response:
        state = json.load(response)
    assert "seeds" not in state["_meta"]
    assert state["tasks"][0]["workspace_tail"] == {"latest_progress": "red test seen"}


def test_the_agent_client_read_leaves_out_seeds_and_work_folder_tails(served):
    with_work_folder()
    state = ledger.call(SLUG)
    assert "seeds" not in state["_meta"]
    assert "workspace_tail" not in state["tasks"][0]
    assert state["_meta"]["rev"] > 0 and state["_meta"]["events"]
