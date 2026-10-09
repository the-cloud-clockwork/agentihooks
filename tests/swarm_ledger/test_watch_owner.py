import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "swarm_ledger"
sys.path.insert(0, str(SCRIPTS))
import ledger_core as core  # noqa: E402
import ledger_gate as gate  # noqa: E402
import new_ledger  # noqa: E402
import watch_ledger  # noqa: E402

from scripts.inbox import seen  # noqa: E402
from scripts.swarm_ledger.repository import repository  # noqa: E402
from tests.swarm_ledger import legacy_page  # noqa: E402

SLUG = "watchowner-2026-01-01"
MASTER = "watchowner-master-1"
ENG = "watchowner-eng-1"
CI = "watchowner-ci-1"


@pytest.fixture
def ledger(monkeypatch):
    monkeypatch.setattr(seen, "marks_for", lambda slug, environ=None: None)
    content = {"title": "Demo", "overview": "o", "sources": [], "phases": [{"title": "one", "description": "d"}]}
    html_path, json_path = core.paths(SLUG)
    html_path.write_text(legacy_page.render(new_ledger.build_doc(content), SLUG, 8765), encoding="utf-8")
    json_path.unlink(missing_ok=True)
    core.sync(SLUG)
    core.sync(
        SLUG,
        ops=[
            {"op": "join", "id": "j-1", "by": MASTER, "role": "orchestrator"},
            {"op": "join", "id": "j-2", "by": ENG},
            {"op": "join", "id": "j-3", "by": CI},
            {"op": "task_add", "id": "t-1", "by": MASTER, "task": "t1", "title": "Do it", "lane": "eng"},
            {"op": "task_update", "id": "t-2", "by": MASTER, "item": "tasks/t1", "fields": {"claimed_by": ENG}},
        ],
    )
    start = repository.get_document(SLUG)["_meta"]["rev"]
    core.sync(
        SLUG,
        ops=[
            {"op": "add", "thread": "chat", "id": "m-plain", "text": "who picks this up"},
            {"op": "add", "thread": "chat", "id": "m-eng", "text": f"@{ENG} look at the tests"},
            {"op": "add", "thread": "tasks/t1/comments", "id": "c-task", "text": "use the other seam"},
        ],
    )
    return start


def file_snapshot(slug, cursor=None, headers=None):
    yield "snapshot", {"ledger": repository.get_document(slug)}, "c0"


def watch(monkeypatch, capsys, start, *flags):
    def stop(_):
        raise SystemExit

    monkeypatch.setattr(watch_ledger.time, "sleep", stop)
    monkeypatch.setattr(watch_ledger, "stream", file_snapshot)
    monkeypatch.setattr(sys, "argv", ["watch_ledger.py", SLUG, "--since-rev", str(start), *flags])
    with pytest.raises(SystemExit):
        watch_ledger.main()
    out = capsys.readouterr().out
    return {cid for cid in ("m-plain", "m-eng", "c-task") if f"[{cid}]" in out}


def test_an_unaddressed_chat_line_shows_only_in_the_master_watch(ledger, monkeypatch, capsys):
    assert watch(monkeypatch, capsys, ledger, "--as", MASTER) == {"m-plain"}


def test_an_addressed_chat_line_and_a_claimed_task_comment_show_only_for_the_engineer(ledger, monkeypatch, capsys):
    assert watch(monkeypatch, capsys, ledger, "--as", ENG) == {"m-eng", "c-task"}
    assert watch(monkeypatch, capsys, ledger, "--as", CI) == set()


def test_all_still_prints_every_operator_event(ledger, monkeypatch, capsys):
    assert watch(monkeypatch, capsys, ledger, "--as", CI, "--all") == {"m-plain", "m-eng", "c-task"}


def test_the_stop_gate_owes_a_claimed_task_comment_to_its_claimer_not_the_master(ledger):
    state = repository.get_document(SLUG)

    def owed(name):
        return {e.get("id") for e in gate.unhandled_for(state["_meta"], name, state["tasks"])}

    assert "c-task" in owed(ENG) and "c-task" not in owed(MASTER)
