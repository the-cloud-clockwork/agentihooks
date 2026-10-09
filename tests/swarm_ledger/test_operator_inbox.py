import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "swarm_ledger"
sys.path.insert(0, str(SCRIPTS))
import ledger_core as core  # noqa: E402
import ledger_hook  # noqa: E402
import ledger_server  # noqa: E402
import new_ledger  # noqa: E402
import watch_ledger  # noqa: E402

from scripts.inbox import seen  # noqa: E402
from scripts.inbox.store import InboxStore  # noqa: E402
from scripts.swarm.store import RedisStore, SwarmConfig  # noqa: E402
from scripts.swarm_ledger.repository import repository  # noqa: E402
from tests.swarm_ledger import legacy_page  # noqa: E402

pytestmark = pytest.mark.xdist_group("fakeredis")

SLUG = "opmail-2026-01-01"


@pytest.fixture
def inbox(monkeypatch):
    import fakeredis

    box = InboxStore(fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True))
    RedisStore(box.redis).create(SwarmConfig(SLUG, "/repo", 1, 0))
    monkeypatch.setattr("scripts.inbox.store.connect", lambda environ=None: box)
    monkeypatch.setattr(seen, "marks_for", lambda slug, environ=None: seen.SeenMarks(box.redis))
    return box


def make_ledger():
    content = {"title": "Demo", "overview": "o", "sources": [], "phases": [{"title": "one", "description": "d"}]}
    html_path, json_path = core.paths(SLUG)
    html_path.write_text(legacy_page.render(new_ledger.build_doc(content), SLUG, 8765), encoding="utf-8")
    json_path.unlink(missing_ok=True)
    state, _ = core.sync(SLUG)
    return state["phases"][0]["id"]


def comment(phase, cid):
    return {"op": "add", "id": cid, "thread": f"phases/{phase}/comments", "text": "operator says hi"}


def test_an_operator_write_through_the_server_becomes_one_item_for_the_master(inbox):
    phase = make_ledger()
    state, _ = core.sync(SLUG, ops=[comment(phase, "c-1")])
    [item] = ledger_server.relay_to_inbox(SLUG, state)
    assert (item.address, item.sender) == (f"master@{SLUG}", "operator") and "operator says hi" in item.text
    assert ledger_server.relay_to_inbox(SLUG, state) == []
    assert len(inbox.pending_items(f"master@{SLUG}")) == 1


def test_an_agent_write_through_the_server_sends_nothing(inbox):
    phase = make_ledger()
    core.sync(SLUG, ops=[{"op": "join", "id": "j", "by": "eng"}])
    state, _ = core.sync(SLUG, ops=[{**comment(phase, "c-2"), "by": "eng"}])
    assert ledger_server.relay_to_inbox(SLUG, state) == []


def hook_state(*revs):
    events = [
        {
            "rev": r,
            "at": r,
            "by": "operator",
            "kind": "comment added",
            "target": "phases/p1",
            "id": f"c-{r}",
            "text": "x",
        }
        for r in revs
    ]
    return {"_meta": {"members": {"boss": {"role": "orchestrator", "handled_rev": 0}}, "events": events}}


def run_hook(tmp_path, capsys, state):
    session = ledger_hook.new_session(SLUG, "boss", "orchestrator")
    ledger_hook.on_tool({"tool_name": "Read"}, session, state, tmp_path / "s.json")
    out = capsys.readouterr().out
    return json.loads(out)["hookSpecificOutput"]["additionalContext"] if out else ""


def test_the_ledger_hook_skips_a_write_the_inbox_already_showed(inbox, tmp_path, capsys):
    shown = hook_state(5)["_meta"]["events"][0]
    seen.SeenMarks(inbox.redis).mark("boss", seen.write_ref(SLUG, shown))
    assert run_hook(tmp_path, capsys, hook_state(5)) == ""
    text = run_hook(tmp_path, capsys, hook_state(5, 6))
    assert "c-6" in text and "c-5" not in text


def test_the_ledger_watch_skips_a_write_already_shown_and_marks_what_it_prints(inbox, monkeypatch, capsys):
    phase = make_ledger()
    start = repository.get_document(SLUG)["_meta"]["rev"]
    state, _ = core.sync(SLUG, ops=[comment(phase, "c-1"), comment(phase, "c-2")])
    first = next(e for e in state["_meta"]["events"] if e.get("id") == "c-1")
    marks = seen.SeenMarks(inbox.redis)
    marks.mark("boss", seen.write_ref(SLUG, first))

    def stop(_):
        raise SystemExit

    monkeypatch.setattr(watch_ledger.time, "sleep", stop)
    monkeypatch.setattr(
        watch_ledger, "stream", lambda slug, cursor=None, headers=None: iter([("snapshot", {"ledger": state}, "c0")])
    )
    monkeypatch.setattr(sys, "argv", ["watch_ledger.py", SLUG, "--as", "boss", "--since-rev", str(start)])
    with pytest.raises(SystemExit):
        watch_ledger.main()
    out = capsys.readouterr().out
    assert "[c-2]" in out and "[c-1]" not in out
    second = next(e for e in state["_meta"]["events"] if e.get("id") == "c-2")
    assert marks.seen("boss", seen.write_ref(SLUG, second))
