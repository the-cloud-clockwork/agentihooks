import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from scripts.swarm_ledger import ledger, ledger_server, ledger_workspace, new_ledger
from scripts.swarm_ledger import ledger_core as core
from scripts.swarm_ledger.repository import repository
from tests.swarm_ledger import legacy_page  # noqa: E402

SLUG = "lean-reads-2026-01-01"
pytestmark = pytest.mark.xdist_group("fakeredis")


@pytest.fixture(autouse=True)
def lean_ledger_dir(ledger_dir, monkeypatch):
    monkeypatch.setattr(core, "LEDGER_DIR", ledger_dir)


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
    html_path.write_text(legacy_page.render(doc, SLUG, 8765), encoding="utf-8")
    json_path.unlink(missing_ok=True)
    return core.sync(SLUG)[0]


def say(n):
    return core.sync(SLUG, ops=[{"op": "add", "thread": "chat", "id": f"m{n}", "text": f"note {n}"}])[0]


def test_a_read_that_changes_nothing_writes_nothing():
    make_ledger()
    say(1)
    before = repository.get_document(SLUG)
    for _ in range(10):
        core.sync(SLUG)
    assert repository.get_document(SLUG) == before


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


def test_the_page_read_leaves_published_work_folder_lines_to_the_task_read(served, monkeypatch):
    import fakeredis

    from scripts.swarm import commands
    from scripts.swarm.store import RedisStore, SwarmConfig

    with_work_folder()
    (ledger_workspace.folder(SLUG, "t1") / "progress.md").write_text("local copy differs\n")
    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig(SLUG, "/hive", 0, 0))
    commands.publish(store, SLUG, {}, {"t1": {"latest_progress": "red test seen"}})
    monkeypatch.setattr(ledger_server, "swarm_store", lambda: store)
    token = legacy_page.stored_token(core.paths(SLUG)[0])
    request = urllib.request.Request(f"{served}/api/{SLUG}", headers={"X-Ledger-Token": token})
    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(request)
    assert error.value.code == 410
    request = urllib.request.Request(
        f"{served}/api/v1/ledgers/{SLUG}/tasks/t1/workspace", headers={"X-Ledger-Token": token}
    )
    with urllib.request.urlopen(request) as response:
        assert json.load(response)["data"] == {"latest_progress": "red test seen"}


def test_the_agent_client_read_leaves_out_seeds_and_work_folder_tails(served):
    with_work_folder()
    state = ledger.call(SLUG)
    assert "seeds" not in state["_meta"]
    assert "workspace_tail" not in state["tasks"][0]
    assert state["_meta"]["rev"] > 0 and state["_meta"]["events"]


def test_the_exported_document_stays_indented_and_keeps_text_as_written(tmp_path):
    from scripts.swarm_ledger import storage_migration

    make_ledger()
    core.sync(SLUG, ops=[{"op": "add", "thread": "chat", "id": "m1", "text": "café ready"}])
    out = tmp_path / "export.json"
    storage_migration.export(SLUG, out)
    text = out.read_text(encoding="utf-8")
    assert text == json.dumps(json.loads(text), indent=2, ensure_ascii=False) + "\n"
    assert "café ready" in text


def test_an_agent_write_returns_the_agent_view_with_its_result(served):
    with_work_folder()
    state = ledger.call(SLUG, [{"op": "join", "id": "j1", "by": "eng"}])
    assert state["rejected"] == []
    assert ledger.resource(SLUG, "members/eng")["id"] == "eng"
    assert "seeds" not in state["_meta"] and "tasks" not in state


def test_the_whole_document_endpoint_is_gone_from_any_position_in_the_query(served):
    with_work_folder()
    token = legacy_page.stored_token(core.paths(SLUG)[0])
    request = urllib.request.Request(f"{served}/api/{SLUG}?x=1&view=agent", headers={"X-Ledger-Token": token})
    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(request)
    assert error.value.code == 410
