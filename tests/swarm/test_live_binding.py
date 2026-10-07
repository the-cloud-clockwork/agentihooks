import json
from dataclasses import replace

import pytest

from scripts.swarm import live_binding
from scripts.swarm.store import AgentRecord

EVENTS = (
    "SessionStart",
    "SessionEnd",
    "PreToolUse",
    "PostToolUse",
    "Stop",
    "UserPromptSubmit",
    "SubagentStart",
    "SubagentStop",
    "PreCompact",
    "PermissionRequest",
)


@pytest.fixture
def mounted(tmp_path):
    home = tmp_path / "engineer" / "claude"
    home.mkdir(parents=True)
    hooks = {event: [{"hooks": [{"type": "command", "command": "python3 -m hooks"}]}] for event in EVENTS}
    (home / "settings.json").write_text(json.dumps({"hooks": hooks}))
    agent = AgentRecord(
        "engineer",
        "eng",
        "one",
        harness="claude",
        profile="engineer",
        model="opus",
        effort="high",
        account="team",
        seat="eng-1@sw",
        pane_id="pane",
        profile_decision={"validation": {"home": str(home), "model": "opus", "effort": "high"}},
    )
    proc = tmp_path / "proc"
    root = proc / "42"
    root.mkdir(parents=True)
    (root / "comm").write_text("claude")
    (root / "cmdline").write_bytes(b"claude\0--model\0opus\0--effort\0high\0")
    (root / "environ").write_bytes(
        f"CLAUDE_CONFIG_DIR={home}\0AGENTIHOOKS_PROFILE=engineer\0AH_CC_TOKEN_team=private-value\0".encode()
    )
    return agent, proc, home


def test_matching_process_facts_exclude_credential_values(mounted):
    agent, proc, home = mounted
    facts = live_binding.read(agent, 42, proc)
    assert facts == {
        "harness": "claude",
        "home": str(home),
        "profile": "engineer",
        "model": "opus",
        "effort": "high",
        "account": "team",
        "hooks": True,
    }
    assert live_binding.compare(agent, facts) == {}
    assert "private-value" not in json.dumps(facts)


@pytest.mark.parametrize("field", ["harness", "home", "profile", "model", "effort", "account", "hooks"])
def test_each_field_mismatch_is_named(mounted, field):
    agent, proc, _ = mounted
    facts = live_binding.read(agent, 42, proc)
    facts[field] = False if field == "hooks" else "wrong"
    differences = live_binding.compare(agent, facts)
    assert list(differences) == [field]
    assert differences[field]["actual"] == facts[field]


@pytest.mark.parametrize("event", EVENTS)
def test_missing_lifecycle_hook_is_refused(mounted, event):
    agent, proc, home = mounted
    path = home / "settings.json"
    data = json.loads(path.read_text())
    del data["hooks"][event]
    path.write_text(json.dumps(data))
    assert live_binding.compare(agent, live_binding.read(agent, 42, proc)) == {
        "hooks": {"expected": True, "actual": False}
    }


@pytest.mark.parametrize("data", [{}, {"hooks": {}}, {"disableAllHooks": True}, {"hooks": {"SessionStart": []}}])
def test_missing_or_disabled_hooks_are_refused(mounted, data):
    agent, proc, home = mounted
    (home / "settings.json").write_text(json.dumps(data))
    assert live_binding.read(agent, 42, proc)["hooks"] is False


def test_removed_hooks_are_read_again(mounted):
    agent, proc, home = mounted
    assert live_binding.read(agent, 42, proc)["hooks"] is True
    (home / "settings.json").unlink()
    assert live_binding.read(agent, 42, proc)["hooks"] is False


def test_original_launch_model_remains_expected_after_telemetry_switch(mounted):
    agent, proc, _ = mounted
    switched = replace(agent, model="sonnet", effort="low")
    assert live_binding.compare(switched, live_binding.read(agent, 42, proc)) == {}
    assert live_binding.assignment(switched)["model"] == "opus"
    assert live_binding.assignment(switched)["effort"] == "high"


def test_missing_process_is_a_named_failure(mounted):
    agent, proc, _ = mounted
    assert live_binding.read(agent, 99, proc) == {"process": False}
    assert live_binding.compare(agent, {"process": False}) == {"process": {"expected": True, "actual": False}}


def test_model_and_effort_require_native_flags(mounted):
    agent, proc, _ = mounted
    root = proc / "42"
    with (root / "environ").open("ab") as stream:
        stream.write(b"AGENTIHOOKS_RUN_MODEL=opus\0AGENTIHOOKS_RUN_EFFORT=high\0")
    (root / "cmdline").write_bytes(b"claude\0")
    assert set(live_binding.compare(agent, live_binding.read(agent, 42, proc))) == {"model", "effort"}


def test_codex_home_hooks_and_native_effort(mounted):
    agent, proc, home = mounted
    agent = replace(
        agent,
        harness="codex",
        model="gpt-6.1-sol",
        account="default",
        profile_decision={"validation": {"home": str(home), "model": "gpt-6.1-sol", "effort": "high"}},
    )
    root = proc / "42"
    (root / "comm").write_text("codex")
    (root / "environ").write_bytes(f"CODEX_HOME={home}\0AGENTIHOOKS_PROFILE=engineer\0".encode())
    (root / "cmdline").write_bytes(b'codex\0-m\0gpt-6.1-sol\0-c\0model_reasoning_effort="high"\0')
    (home / "config.toml").write_text("[features]\nhooks = true\n")
    hooks = {event: [{"hooks": [{"command": "/operator/.codex/agentihooks-hook.sh"}]}] for event in EVENTS}
    (home / "hooks.json").write_text(json.dumps({"hooks": hooks}))
    assert live_binding.compare(agent, live_binding.read(agent, 42, proc)) == {}
    (home / "config.toml").write_text("[features]\nhooks = false\n")
    assert live_binding.read(agent, 42, proc)["hooks"] is False


@pytest.mark.parametrize("content", ["broken", "[]"])
def test_unparseable_hook_configuration_is_refused(mounted, content):
    agent, proc, home = mounted
    (home / "settings.json").write_text(content)
    assert live_binding.read(agent, 42, proc)["hooks"] is False


def test_foreign_hooks_do_not_count_as_agentihooks(mounted):
    agent, proc, home = mounted
    hooks = {event: [{"hooks": [{"command": "echo ready"}]}] for event in EVENTS}
    (home / "settings.json").write_text(json.dumps({"hooks": hooks}))
    assert live_binding.read(agent, 42, proc)["hooks"] is False


@pytest.mark.parametrize("command", ["python -m hooks.mcp", "echo python -m hooks", "echo agentihooks-hook.sh"])
def test_non_lifecycle_commands_do_not_count_as_hooks(mounted, command):
    agent, proc, home = mounted
    data = json.loads((home / "settings.json").read_text())
    for groups in data["hooks"].values():
        groups[0]["hooks"][0]["command"] = command
    (home / "settings.json").write_text(json.dumps(data))
    assert live_binding.read(agent, 42, proc)["hooks"] is False


@pytest.fixture
def ticking():
    import fakeredis

    from scripts.swarm.store import RedisStore, SwarmConfig
    from tests.swarm.test_tick import FakeLedger, FakeRuntime

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("sw", "/repo", max_eng=1, max_ci=0))
    runtime = FakeRuntime()
    runtime.bindings = lambda agents: {a.name: {**live_binding.assignment(a), "hooks": True} for a in agents}
    runtime.pane_open = lambda agent: agent.pane_id not in runtime.closed
    ledger = FakeLedger([{"id": "one"}])
    return store, runtime, ledger


@pytest.mark.parametrize("field", ["home", "profile", "model", "effort", "account", "hooks", "harness"])
def test_tick_reclaims_each_mismatched_agent_on_its_task(ticking, field):
    from scripts.swarm.tick import tick

    store, runtime, ledger = ticking
    tick("sw", store, ledger, runtime, 100)
    old = next(a for a in store.agents("sw") if a.lane == "eng")
    runtime.bindings = lambda agents: {
        a.name: {
            **live_binding.assignment(a),
            "hooks": True,
            **({field: False if field == "hooks" else "wrong"} if a.name == old.name else {}),
        }
        for a in agents
    }
    actions = tick("sw", store, ledger, runtime, 200)
    new = next(a for a in store.agents("sw") if a.lane == "eng")
    assert new.name != old.name
    assert new.task == old.task
    assert new.seat == old.seat
    assert old.name in runtime.killed
    assert old.pane_id in runtime.closed
    assert runtime.tasks[-1]["launch_assignment"]["model"] == old.model
    assert runtime.tasks[-1]["launch_assignment"]["account"] == old.account
    assert any(field in action for action in actions)
    found = live_binding.findings(store, "sw")
    assert any(f.subject == f"{old.name}/{field}" for f in found)
    assert ledger.rows["one"]["claimed_by"] == new.name


def test_matching_agents_are_checked_on_every_pass(ticking):
    from scripts.swarm.tick import tick

    store, runtime, ledger = ticking
    tick("sw", store, ledger, runtime, 100)
    before = store.agents("sw")
    tick("sw", store, ledger, runtime, 200)
    tick("sw", store, ledger, runtime, 300)
    assert store.agents("sw") == before
    reports = store.redis.hgetall(store.key("sw", "live-bindings"))
    assert set(reports) == {a.name for a in before}
    assert all(json.loads(raw)["at"] == 300 for raw in reports.values())
    assert all(json.loads(raw)["state"] == "matching" for raw in reports.values())
    assert not runtime.killed


def test_failed_retirement_keeps_claim_and_retries(ticking):
    from scripts.swarm.tick import tick

    store, runtime, ledger = ticking
    tick("sw", store, ledger, runtime, 100)
    old = next(a for a in store.agents("sw") if a.lane == "eng")
    runtime.stuck.add(old.name)
    runtime.bindings = lambda agents: {
        a.name: {**live_binding.assignment(a), "hooks": a.name != old.name} for a in agents
    }
    tick("sw", store, ledger, runtime, 200)
    assert old in store.agents("sw")
    assert ledger.rows["one"]["claimed_by"] == old.name
    runtime.stuck.clear()
    tick("sw", store, ledger, runtime, 300)
    assert old not in store.agents("sw")


def test_finished_pane_is_closed_and_reported(ticking):
    from scripts.swarm.tick import tick

    store, runtime, ledger = ticking
    tick("sw", store, ledger, runtime, 100)
    old = next(a for a in store.agents("sw") if a.lane == "eng")
    store.put_agent("sw", replace(old, state="finished"))
    ledger.rows["one"].update(state="done", done=True)
    tick("sw", store, ledger, runtime, 200)
    assert old.pane_id in runtime.closed
    assert old not in store.agents("sw")
    assert any(f.subject == f"{old.name}/pane" for f in live_binding.findings(store, "sw"))


def test_relaunch_preserves_native_assignment_options(tmp_path, monkeypatch):
    from tests.swarm.test_runtime import _launched

    saved = {"profile": "engineer", "harness": "claude", "model": "sonnet", "effort": "medium", "account": "team"}
    passed = _launched(
        tmp_path, monkeypatch, "eng", {"id": "one", "title": "Verify", "profile": "qa", "launch_assignment": saved}
    )
    assert passed[:2] == ["--route", "team"]
    assert passed[passed.index("--model") + 1] == "sonnet"
    assert passed[passed.index("--effort") + 1] == "medium"


def test_runtime_reads_only_processes_named_in_assignments(mounted, monkeypatch):
    from types import SimpleNamespace

    from scripts import terminate_agent
    from scripts.swarm.runtime import HerdrRuntime

    agent, proc, _ = mounted
    monkeypatch.setattr(
        terminate_agent,
        "sessions",
        lambda: [
            SimpleNamespace(name=agent.name, process=SimpleNamespace(pid=42)),
            SimpleNamespace(name="foreign", process=SimpleNamespace(pid=99)),
        ],
    )
    calls = []

    def read(record, pid):
        calls.append((record.name, pid))
        return {"hooks": True}

    monkeypatch.setattr(live_binding, "read", read)
    assert HerdrRuntime().bindings([agent]) == {agent.name: {"hooks": True}}
    assert calls == [(agent.name, 42)]


def test_reports_are_visible_in_swarm_health(ticking):
    from scripts.swarm.status import findings

    store, _, ledger = ticking
    agent = AgentRecord("engineer", "eng", "one", profile="engineer", harness="claude")
    live_binding.record(store, "sw", agent, {**live_binding.assignment(agent), "hooks": False}, 10)
    found = findings(store, "sw", store.config("sw"), ledger.tasks("sw"), [])
    assert any(f["id"] == "live-binding/engineer/hooks" for f in found)
