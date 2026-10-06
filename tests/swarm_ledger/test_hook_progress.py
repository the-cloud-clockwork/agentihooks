import json

import pytest

from scripts.gates import progress

pytestmark = pytest.mark.xdist_group("fakeredis")

SLUG, ME = "hookprogress-2026-01-01", "engineer@abcdef-0001"


@pytest.fixture
def hook(tmp_path, monkeypatch):
    import fakeredis

    from scripts.swarm_ledger import ledger_hook

    marks = progress.Progress(fakeredis.FakeRedis(decode_responses=True), SLUG)
    monkeypatch.setattr(ledger_hook, "SESSIONS", tmp_path)
    monkeypatch.setattr(ledger_hook, "progress_of", lambda session: marks)
    monkeypatch.setattr(ledger_hook, "first_shown", lambda session, owed: owed)
    state = {"_meta": {"members": {ME: {"role": "member", "handled_rev": 0}}, "events": []}, "tasks": []}
    session = ledger_hook.new_session(SLUG, ME, "member")
    sfile = tmp_path / "s.json"

    def call(command="ls", **extra):
        payload = {"tool_name": "Bash", "tool_input": {"command": command}, **extra}
        ledger_hook.on_tool(payload, session, state, sfile)

    call.marks, call.session, call.state, call.sfile, call.module = marks, session, state, sfile, ledger_hook
    return call


def test_a_push_records_the_outcome_and_restarts_the_counters(hook):
    for _ in range(5):
        hook()
    hook("git push -u origin engineer-323133-0115")
    assert hook.marks.read(ME).outcome == "pushed"
    assert hook.session["calls"] == 0


def test_an_opened_pull_request_is_an_outcome(hook):
    hook("gh pr create --base dev --title t --body b")
    assert hook.marks.read(ME).outcome == "pull request opened"


def test_tool_calls_inside_a_sub_agent_are_not_counted(hook):
    hook(agent_id="sub-1", agent_type="code-reviewer")
    hook(agent_id="sub-1", agent_type="code-reviewer")
    assert hook.session["calls"] == 0
    hook()
    assert hook.session["calls"] == 1


def test_the_nudge_waits_while_an_outcome_landed_since_the_last_count(hook, capsys):
    for _ in range(24):
        hook()
    hook.marks.outcome(ME, "task pr", now_ms=1000)
    hook()
    assert capsys.readouterr().out == ""
    assert hook.session["calls"] == 0
    assert hook.session["outcome_at"] == 1000
    assert json.loads(hook.sfile.read_text())["outcome_at"] == 1000


def test_the_nudge_fires_with_no_outcome(hook, capsys):
    for _ in range(25):
        hook()
    assert "25 tool calls without recording progress" in capsys.readouterr().out


def test_an_outcome_already_counted_does_not_restart_again(hook, capsys):
    hook.marks.outcome(ME, "task pr", now_ms=1000)
    hook.session["outcome_at"] = 1000
    for _ in range(25):
        hook()
    assert "25 tool calls" in capsys.readouterr().out


def test_stop_passes_after_an_outcome_and_blocks_without_one(hook, capsys):
    hook.session["calls"] = 12
    hook.module.on_stop({}, hook.session, hook.state, hook.sfile)
    assert json.loads(capsys.readouterr().out)["decision"] == "block"
    hook.marks.outcome(ME, "pushed", now_ms=5)
    hook.session["calls"] = 12
    hook.module.on_stop({}, hook.session, hook.state, hook.sfile)
    assert capsys.readouterr().out == ""
    assert json.loads(hook.sfile.read_text())["calls"] == 0


def test_an_unreadable_progress_signal_reads_as_no_outcome(hook, monkeypatch, capsys):
    def down(session):
        raise ConnectionError("refused")

    monkeypatch.setattr(hook.module, "progress_of", down)
    hook("git push")
    for _ in range(25):
        hook()
    assert "25 tool calls" in capsys.readouterr().out
