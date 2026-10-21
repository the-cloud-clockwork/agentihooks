import json

import pytest

from scripts.gates import progress

SLUG, ME = "hookprogress-2026-01-01", "engineer@abcdef-0001"


@pytest.fixture
def hook(tmp_path, monkeypatch):
    import fakeredis

    from scripts.swarm_ledger import ledger_hook

    marks = progress.Progress(fakeredis.FakeRedis(decode_responses=True), SLUG)
    state = {"_meta": {"members": {ME: {"role": "member", "handled_rev": 0}}, "events": []}, "tasks": []}
    session = ledger_hook.new_session(SLUG, ME, "member")
    monkeypatch.setattr(ledger_hook, "SESSIONS", tmp_path)
    monkeypatch.setattr(ledger_hook, "progress_of", lambda bound: marks if bound is session else None)
    monkeypatch.setattr(ledger_hook, "first_shown", lambda session, owed: owed)
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


def test_an_outcome_restarts_every_counter(hook):
    hook.session.update(calls=9, nudge_calls=7, blocks=2, bypass_posted=True)
    hook("git push")
    assert [hook.session[k] for k in ("calls", "nudge_calls", "blocks", "bypass_posted")] == [0, 0, 0, False]


def test_a_ledger_write_restarts_the_count(hook):
    for _ in range(3):
        hook()
    hook(f"agentihooks ledger --slug {SLUG} --as {ME} comment phases/p1 done")
    assert hook.session["calls"] == 0
    assert hook.marks.read(ME).outcome_at == 0


def test_the_nudge_fires_once_per_window(hook, capsys):
    for _ in range(25):
        hook()
    assert "25 tool calls" in capsys.readouterr().out
    hook()
    assert capsys.readouterr().out == ""


def test_stop_blocks_at_exactly_the_call_limit(hook, capsys):
    hook.session["calls"] = 10
    hook.module.on_stop({}, hook.session, hook.state, hook.sfile)
    assert json.loads(capsys.readouterr().out)["decision"] == "block"


def test_stop_holds_while_plan_mode_is_on(hook, capsys):
    hook.session["calls"] = 12
    hook.module.on_stop({"permission_mode": "plan"}, hook.session, hook.state, hook.sfile)
    assert capsys.readouterr().out == ""
    assert hook.session["blocks"] == 0
    hook.module.on_stop({"permission_mode": "default"}, hook.session, hook.state, hook.sfile)
    assert json.loads(capsys.readouterr().out)["decision"] == "block"


def test_a_first_outcome_at_any_time_is_seen_on_a_fresh_session(hook):
    hook.marks.outcome(ME, "pushed", now_ms=1)
    assert hook.module.outcome_seen(hook.session) is True
    assert hook.session["outcome_at"] == 1


def test_a_failed_progress_signal_is_logged(hook, monkeypatch):
    lines = []

    def down(session):
        raise ConnectionError("refused")

    monkeypatch.setattr(hook.module, "progress_of", down)
    monkeypatch.setattr(hook.module, "log", lines.append)
    hook.module.note_outcome(hook.session, "pushed")
    assert hook.module.outcome_seen(hook.session) is False
    assert lines == ["outcome not recorded: refused", "progress unreadable: refused"]


def test_the_progress_signal_reads_the_sessions_swarm(monkeypatch):
    from scripts.swarm import store
    from scripts.swarm_ledger import ledger_hook

    client = object()
    monkeypatch.setattr(store, "redis_client", lambda: client)
    marks = ledger_hook.progress_of(ledger_hook.new_session(SLUG, ME, "member"))
    assert (marks.redis, marks.slug) == (client, SLUG)


@pytest.mark.parametrize("kind", ["checks", "reply", "task", None])
def test_a_live_declared_wait_holds_the_progress_budget(hook, monkeypatch, capsys, kind):
    from scripts.swarm import idle, store

    redis = hook.marks.redis
    monkeypatch.setattr(store, "redis_client", lambda: redis)
    now = int(hook.module.time.time() * 1000)
    idle.declare_wait(redis, SLUG, ME, now + 60_000, "waiting for the declared target", now, on=kind)
    for _ in range(30):
        hook()
    hook.module.on_stop({}, hook.session, hook.state, hook.sfile)
    assert capsys.readouterr().out == ""

    idle.end_wait(redis, SLUG, ME, now + 1)
    for _ in range(24):
        hook()
    assert capsys.readouterr().out == ""
    hook()
    assert "25 tool calls without recording progress" in capsys.readouterr().out
    hook.module.on_stop({}, hook.session, hook.state, hook.sfile)
    assert "tool calls since you last recorded progress" in capsys.readouterr().out


def test_a_live_wait_clears_an_existing_stop_progress_demand(hook, monkeypatch, capsys):
    from scripts.swarm import idle, store

    redis = hook.marks.redis
    monkeypatch.setattr(store, "redis_client", lambda: redis)
    now = int(hook.module.time.time() * 1000)
    hook.session.update(calls=30, nudge_calls=25, blocks=1, bypass_posted=True)
    idle.declare_wait(redis, SLUG, ME, now + 60_000, "awaiting checks", now, on="checks")
    hook.module.on_stop({}, hook.session, hook.state, hook.sfile)
    assert capsys.readouterr().out == ""
    assert [hook.session[k] for k in ("calls", "nudge_calls", "blocks", "bypass_posted")] == [0, 0, 0, False]


@pytest.mark.parametrize("until_offset", [-1, 0])
def test_an_expired_wait_keeps_the_progress_demand(hook, monkeypatch, capsys, until_offset):
    from scripts.swarm import idle, store

    redis = hook.marks.redis
    monkeypatch.setattr(store, "redis_client", lambda: redis)
    now = 1_792_042_452_000
    monkeypatch.setattr(hook.module.time, "time", lambda: now / 1000)
    redis.set(idle.key(SLUG, "wait", ME), json.dumps({"until": now + until_offset, "on": "checks"}))
    for _ in range(25):
        hook()
    assert "25 tool calls without recording progress" in capsys.readouterr().out
    hook.module.on_stop({}, hook.session, hook.state, hook.sfile)
    assert "tool calls since you last recorded progress" in capsys.readouterr().out


@pytest.mark.parametrize("other_slug,other_name", [("another-swarm", ME), (SLUG, "another-agent")])
def test_another_agents_wait_keeps_the_progress_demand(hook, monkeypatch, capsys, other_slug, other_name):
    from scripts.swarm import idle, store

    redis = hook.marks.redis
    monkeypatch.setattr(store, "redis_client", lambda: redis)
    now = int(hook.module.time.time() * 1000)
    idle.declare_wait(redis, other_slug, other_name, now + 60_000, "awaiting checks", now, on="checks")
    for _ in range(25):
        hook()
    assert "25 tool calls without recording progress" in capsys.readouterr().out
    hook.module.on_stop({}, hook.session, hook.state, hook.sfile)
    assert "tool calls since you last recorded progress" in capsys.readouterr().out


def test_a_wait_one_millisecond_in_the_future_still_holds(hook, monkeypatch, capsys):
    from scripts.swarm import idle, store

    redis = hook.marks.redis
    monkeypatch.setattr(store, "redis_client", lambda: redis)
    now = 1_792_042_452_000
    monkeypatch.setattr(hook.module.time, "time", lambda: now / 1000)
    redis.set(idle.key(SLUG, "wait", ME), json.dumps({"until": now + 1, "on": "checks"}))
    hook.session["calls"] = 30
    hook.module.on_stop({}, hook.session, hook.state, hook.sfile)
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("raw", ["not-json", "{}", '{"on": "checks"}'])
def test_an_unreadable_or_incomplete_wait_keeps_the_progress_demand(hook, monkeypatch, capsys, raw):
    from scripts.swarm import idle, store

    redis = hook.marks.redis
    monkeypatch.setattr(store, "redis_client", lambda: redis)
    messages = []
    monkeypatch.setattr(hook.module, "log", messages.append)
    redis.set(idle.key(SLUG, "wait", ME), raw)
    for _ in range(25):
        hook()
    assert "25 tool calls without recording progress" in capsys.readouterr().out
    hook.module.on_stop({}, hook.session, hook.state, hook.sfile)
    assert "tool calls since you last recorded progress" in capsys.readouterr().out
    if raw == "not-json":
        assert messages[0] == "wait unreadable: Expecting value: line 1 column 1 (char 0)"
