import pytest

import hooks.context.swarm_heartbeat as swarm_heartbeat
from hooks import hook_manager

pytestmark = pytest.mark.xdist_group("fakeredis")


@pytest.fixture
def calls(monkeypatch):
    seen = []
    monkeypatch.setattr(swarm_heartbeat, "beat", lambda state: seen.append(("beat", state)))
    monkeypatch.setattr(swarm_heartbeat, "heard", lambda prompt: seen.append(("heard", prompt)))
    return seen


def test_a_prompt_beats_working_and_records_its_text(calls):
    hook_manager._swarm_heartbeat("working", "hold on")
    assert calls == [("beat", "working"), ("heard", "hold on")]


def test_a_tool_call_beats_without_recording_a_prompt(calls):
    hook_manager._swarm_heartbeat("idle")
    assert calls == [("beat", "idle")]


@pytest.mark.parametrize("payload, prompt", [({"prompt": "hold on"}, "hold on"), ({}, "")])
def test_user_prompt_submit_hands_its_prompt_to_the_heartbeat(monkeypatch, payload, prompt):
    seen = []
    monkeypatch.delenv("AGENTIHOOKS_SWARM", raising=False)
    monkeypatch.setattr(hook_manager, "_swarm_heartbeat", lambda *args: seen.append(args))
    hook_manager.on_user_prompt_submit({"session_id": "s1", "cwd": "/", **payload})
    assert seen[0] == ("working", prompt)


@pytest.mark.parametrize("model", [{"model": "gpt-6.1-sol"}, {}])
@pytest.mark.parametrize(
    "handler, event, state", [("on_pre_tool_use", "PreToolUse", "working"), ("on_stop", "Stop", "idle")]
)
def test_a_hook_beats_and_reports_the_model_its_payload_names(monkeypatch, handler, event, state, model):
    seen = []
    monkeypatch.delenv("AGENTIHOOKS_SWARM", raising=False)
    monkeypatch.setattr(swarm_heartbeat, "beat", lambda beaten: seen.append(("beat", beaten)))
    monkeypatch.setattr(swarm_heartbeat, "report", lambda reported: seen.append(("report", reported)))
    payload = {"hook_event_name": event, "session_id": "s1", "cwd": "/", "transcript_path": "", **model}
    getattr(hook_manager, handler)({**payload, "tool_name": "Bash", "tool_input": {"command": "true"}})
    assert seen == [("beat", state), ("report", model.get("model", ""))]


SWARM_ENV = {"AGENTIHOOKS_SWARM": "demo", "AGENTIHOOKS_AGENT_NAME": "engineer@abcdef-0001"}


def shell(command):
    return {"tool_name": "Bash", "tool_input": {"command": command}}


@pytest.mark.parametrize(
    "command, kind",
    [("cd /w\ngit push -u origin HEAD", "pushed"), ("gh pr create --base dev --fill", "pull request opened")],
)
def test_a_pinned_worker_push_or_opened_pull_request_records_its_outcome(command, kind):
    import fakeredis

    from scripts.gates.progress import Mark, Progress

    redis = fakeredis.FakeRedis(decode_responses=True)
    assert swarm_heartbeat.outcome(shell(command), SWARM_ENV, redis, now_ms=77) is True
    assert Progress(redis, "demo").read("engineer@abcdef-0001") == Mark(77, kind, 0)


@pytest.mark.parametrize(
    "payload, env",
    [
        (shell("git status"), SWARM_ENV),
        ({"tool_name": "Read", "tool_input": {"command": "git push"}}, SWARM_ENV),
        (shell("git push"), {"AGENTIHOOKS_SWARM": "demo"}),
    ],
)
def test_other_calls_and_unpinned_sessions_record_nothing(payload, env):
    import fakeredis

    redis = fakeredis.FakeRedis(decode_responses=True)
    assert swarm_heartbeat.outcome(payload, env, redis, now_ms=77) is False
    assert redis.keys() == []


def test_post_tool_use_hands_every_harness_call_to_the_outcome_record(monkeypatch):
    seen = []
    monkeypatch.setenv("AGENTIHOOKS_TARGET", "codex")
    monkeypatch.delenv("AGENTIHOOKS_SWARM", raising=False)
    monkeypatch.setattr(swarm_heartbeat, "outcome", lambda payload: seen.append(payload))
    payload = {"hook_event_name": "PostToolUse", "session_id": "s1", "cwd": "/", "transcript_path": ""}
    hook_manager.on_post_tool_use({**payload, **shell("git push")})
    assert [p["tool_input"] for p in seen] == [{"command": "git push"}]


def test_a_codex_push_payload_records_the_outcome_end_to_end(monkeypatch):
    import fakeredis

    from hooks.targets.normalizer import normalize_payload
    from scripts.gates.progress import Progress

    redis = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setenv("AGENTIHOOKS_TARGET", "codex")
    for key, value in SWARM_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr("scripts.swarm.store.redis_client", lambda env=None: redis)
    raw = {"hook_event_name": "PostToolUse", "session_id": "s1", "cwd": "/", "tool_response": "ok"}
    payload = normalize_payload({**raw, **shell("cd /w\ngit push -u origin HEAD")})
    hook_manager._swarm_outcome(payload)
    assert Progress(redis, "demo").read("engineer@abcdef-0001").outcome == "pushed"
