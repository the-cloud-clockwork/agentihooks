import json
from dataclasses import replace

import pytest

from scripts.swarm import live_binding
from scripts.swarm.store import AgentRecord

pytestmark = pytest.mark.xdist_group("fakeredis")

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
    (home / ".agentihooks-render.json").write_text(json.dumps({"chain": ["engineer", "brain"], "overlays": ["brain"]}))
    facts = live_binding.read(agent, 42, proc)
    assert facts == {
        "harness": "claude",
        "home": str(home),
        "profile": "engineer",
        "model": "opus",
        "effort": "high",
        "account": "team",
        "hooks": True,
        "chain": ["engineer", "brain"],
        "overlays": ["brain"],
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


@pytest.mark.parametrize("harness", ["claude", "codex"])
def test_the_rendered_profile_chain_is_read_from_the_home(mounted, harness):
    agent, proc, home = mounted
    data = {"chain": ["anton-base", "package:engineer", "engineer", "brain"], "overlays": ["brain"]}
    if harness == "codex":
        root = proc / "42"
        (root / "comm").write_text("codex")
        (root / "cmdline").write_bytes(b"codex\0--model\0gpt-6.1-sol\0")
        (root / "environ").write_bytes(f"CODEX_HOME={home}\0AGENTIHOOKS_PROFILE=engineer\0".encode())
        data = {"render": data, "operator": "digest"}
    (home / ".agentihooks-render.json").write_text(json.dumps(data))
    facts = live_binding.read(agent, 42, proc)
    assert facts["chain"] == ["anton-base", "package:engineer", "engineer", "brain"]
    assert facts["overlays"] == ["brain"]


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
    facts = live_binding.read(agent, 42, proc)
    assert (facts["model"], facts["effort"]) == ("", "")
    assert set(live_binding.compare(agent, facts)) == {"model", "effort"}


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
    (home / "settings.json").unlink()
    (home / "config.toml").write_text("[features]\nhooks = true\n")
    wrapper = home / "agentihooks-hook.sh"
    wrapper.touch()
    wrapper.chmod(0o700)
    hooks = {event: [{"hooks": [{"type": "command", "command": str(wrapper)}]}] for event in EVENTS}
    (home / "hooks.json").write_text(json.dumps({"hooks": hooks}))
    assert live_binding.compare(agent, live_binding.read(agent, 42, proc)) == {}
    (home / "config.toml").write_text("[features]\nhooks = false\n")
    assert live_binding.read(agent, 42, proc)["hooks"] is False


def test_flagless_claude_session_reads_model_and_effort_from_its_home(mounted):
    agent, proc, home = mounted
    path = home / "settings.json"
    data = json.loads(path.read_text())
    path.write_text(json.dumps({**data, "model": "opus", "effortLevel": "high"}))
    (proc / "42" / "cmdline").write_bytes(b"claude\0")
    facts = live_binding.read(agent, 42, proc)
    assert (facts["model"], facts["effort"]) == ("opus", "high")
    assert live_binding.compare(agent, facts) == {}
    (proc / "42" / "cmdline").write_bytes(b"claude\0--model\0sonnet\0--effort\0low\0")
    facts = live_binding.read(agent, 42, proc)
    assert (facts["model"], facts["effort"]) == ("sonnet", "low")
    assert set(live_binding.compare(agent, facts)) == {"model", "effort"}


def test_flagless_codex_session_reads_model_and_effort_from_its_home(mounted):
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
    (root / "cmdline").write_bytes(b"codex\0")
    (home / "config.toml").write_text(
        'model = "gpt-6.1-sol"\nmodel_reasoning_effort = "high"\n\n[features]\nhooks = true\n'
    )
    wrapper = home / "agentihooks-hook.sh"
    wrapper.touch()
    wrapper.chmod(0o700)
    hooks = {event: [{"hooks": [{"type": "command", "command": str(wrapper)}]}] for event in EVENTS}
    (home / "hooks.json").write_text(json.dumps({"hooks": hooks}))
    facts = live_binding.read(agent, 42, proc)
    assert (facts["model"], facts["effort"]) == ("gpt-6.1-sol", "high")
    assert live_binding.compare(agent, facts) == {}
    (root / "cmdline").write_bytes(b'codex\0-m\0gpt-7\0-c\0model_reasoning_effort="low"\0')
    facts = live_binding.read(agent, 42, proc)
    assert (facts["model"], facts["effort"]) == ("gpt-7", "low")
    assert set(live_binding.compare(agent, facts)) == {"model", "effort"}


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


@pytest.mark.parametrize("command", ["false && python3 -m hooks", "/missing/agentihooks-hook.sh"])
def test_unexecutable_lifecycle_registration_is_refused(mounted, command):
    agent, proc, home = mounted
    data = json.loads((home / "settings.json").read_text())
    for groups in data["hooks"].values():
        groups[0]["hooks"][0]["command"] = command
    (home / "settings.json").write_text(json.dumps(data))
    assert live_binding.read(agent, 42, proc)["hooks"] is False


def test_prompt_hook_cannot_replace_command_hook(mounted):
    agent, proc, home = mounted
    data = json.loads((home / "settings.json").read_text())
    data["hooks"]["SessionStart"][0]["hooks"][0]["type"] = "prompt"
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
    ledger = FakeLedger([{"id": "one"}])
    return store, runtime, ledger


@pytest.mark.parametrize("field", ["home", "profile", "model", "effort", "account", "hooks", "harness"])
def test_tick_reclaims_each_mismatched_agent_on_its_task(ticking, field):
    from scripts.swarm.tick import tick

    store, runtime, ledger = ticking
    tick("sw", store, ledger, runtime, 100)
    old = replace(next(a for a in store.agents("sw") if a.lane == "eng"), profile="engineer")
    store.put_agent("sw", old)
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
    assert any(a.name == old.name and a.state == "retiring" for a in store.agents("sw"))
    assert ledger.rows["one"]["claimed_by"] == old.name
    runtime.stuck.clear()
    tick("sw", store, ledger, runtime, 300)
    assert old not in store.agents("sw")


def test_partial_retirement_failure_retains_ownership_until_pane_closes(ticking):
    from scripts.swarm.tick import STARTUP_GRACE_MS, tick

    store, runtime, ledger = ticking
    tick("sw", store, ledger, runtime, 100)
    old = next(a for a in store.agents("sw") if a.lane == "eng")
    runtime.bindings = lambda agents: {
        a.name: {**live_binding.assignment(a), "hooks": a.name != old.name} for a in agents if a.name in runtime.live
    }
    retire = runtime.retire

    def partial(agent, homes=()):
        if agent.name == old.name:
            runtime.live.discard(old.name)
            return False
        return retire(agent, homes)

    runtime.retire = partial
    tick("sw", store, ledger, runtime, STARTUP_GRACE_MS + 200)
    assert any(a.name == old.name for a in store.agents("sw"))
    assert ledger.rows["one"]["claimed_by"] == old.name
    assert old.pane_id not in runtime.closed
    runtime.retire = retire
    tick("sw", store, ledger, runtime, STARTUP_GRACE_MS + 300)
    assert all(a.name != old.name for a in store.agents("sw"))
    assert old.pane_id in runtime.closed
    assert ledger.rows["one"]["claimed_by"] != old.name


def test_finished_pane_is_closed_without_a_pane_finding(ticking):
    from scripts.swarm.tick import tick

    store, runtime, ledger = ticking
    tick("sw", store, ledger, runtime, 100)
    old = next(a for a in store.agents("sw") if a.lane == "eng")
    store.put_agent("sw", replace(old, state="finished"))
    ledger.rows["one"].update(state="done", done=True)
    tick("sw", store, ledger, runtime, 200)
    assert old.pane_id in runtime.closed
    assert old not in store.agents("sw")
    assert live_binding.findings(store, "sw") == []


def test_relaunch_preserves_native_assignment_options(tmp_path, monkeypatch):
    from tests.swarm.test_runtime import _launched

    saved = {"profile": "engineer", "harness": "claude", "model": "sonnet", "effort": "medium", "account": "team"}
    passed = _launched(
        tmp_path, monkeypatch, "eng", {"id": "one", "title": "Verify", "profile": "qa", "launch_assignment": saved}
    )
    assert passed[:2] == ["--route", "team"]
    assert passed[passed.index("--model") + 1] == "sonnet"
    assert passed[passed.index("--effort") + 1] == "medium"


def test_relaunch_with_empty_assignment_options_chooses_afresh(tmp_path, monkeypatch):
    from tests.swarm.test_runtime import _launched

    saved = {"profile": "master", "harness": "", "model": "", "effort": "", "account": "", "seat": "master@sw"}
    passed = _launched(tmp_path, monkeypatch, "master", {"id": "master", "title": "Master", "launch_assignment": saved})
    assert "--route" not in passed


def test_runtime_reads_only_processes_named_in_assignments(mounted, monkeypatch):
    from types import SimpleNamespace

    from scripts import terminate_agent
    from scripts.swarm.runtime import HerdrRuntime

    agent, proc, _ = mounted
    monkeypatch.setattr(
        terminate_agent,
        "sessions",
        lambda: [
            SimpleNamespace(
                name=agent.name,
                target="claude",
                status="alive",
                session_id="",
                process=SimpleNamespace(pid=42, start_time=1),
            ),
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


@pytest.mark.parametrize("prefix", ["", "bash ", "sh ", "exec "])
def test_valid_registered_wrapper_forms(mounted, prefix):
    agent, proc, home = mounted
    wrapper = home / "agentihooks-hook.sh"
    wrapper.touch()
    wrapper.chmod(0o700)
    data = json.loads((home / "settings.json").read_text())
    for groups in data["hooks"].values():
        groups[0]["hooks"][0]["command"] = f"{prefix}{wrapper}"
    (home / "settings.json").write_text(json.dumps(data))
    assert live_binding.read(agent, 42, proc)["hooks"] is True


@pytest.mark.parametrize("prefix, valid", [("", False), ("exec ", False), ("bash ", True), ("sh ", True)])
def test_wrapper_execute_permission_matches_invocation(mounted, prefix, valid):
    agent, proc, home = mounted
    wrapper = home / "agentihooks-hook.sh"
    wrapper.touch()
    wrapper.chmod(0o600)
    data = json.loads((home / "settings.json").read_text())
    data["hooks"]["SessionStart"][0]["hooks"][0]["command"] = f"{prefix}{wrapper}"
    (home / "settings.json").write_text(json.dumps(data))
    assert live_binding.read(agent, 42, proc)["hooks"] is valid


@pytest.mark.parametrize("prefix", ["", "exec ", "cd {home} && "])
def test_valid_python_lifecycle_forms(mounted, prefix):
    agent, proc, home = mounted
    data = json.loads((home / "settings.json").read_text())
    for groups in data["hooks"].values():
        groups[0]["hooks"][0]["command"] = prefix.replace("{home}", str(home)) + "python3 -m hooks"
    (home / "settings.json").write_text(json.dumps(data))
    assert live_binding.read(agent, 42, proc)["hooks"] is True


@pytest.mark.parametrize(
    "command",
    [
        "",
        "bash",
        "sh",
        "exec",
        "cd",
        "cd /missing && python3 -m hooks",
        "cd / python3 -m hooks",
        "python3 -m",
        "python3 -m other",
        "python3.12 -m hooks.mcp",
    ],
)
def test_invalid_registered_command_forms(mounted, command):
    agent, proc, home = mounted
    data = json.loads((home / "settings.json").read_text())
    data["hooks"]["SessionStart"][0]["hooks"][0]["command"] = command
    (home / "settings.json").write_text(json.dumps(data))
    assert live_binding.read(agent, 42, proc)["hooks"] is False


def test_disable_all_hooks_overrides_valid_registration(mounted):
    agent, proc, home = mounted
    data = json.loads((home / "settings.json").read_text())
    data["disableAllHooks"] = True
    (home / "settings.json").write_text(json.dumps(data))
    assert live_binding.read(agent, 42, proc)["hooks"] is False


def test_absent_live_environment_fields_are_named(mounted):
    agent, proc, _ = mounted
    (proc / "42" / "environ").write_bytes(b"AH_CC_TOKEN_team=private-value\0")
    facts = live_binding.read(agent, 42, proc)
    assert facts["home"] == ""
    assert facts["profile"] == ""
    assert facts["hooks"] is False
    assert live_binding.compare(agent, facts)["home"]["actual"] == ""


def test_invalid_utf8_in_unrelated_process_argument_does_not_hide_launch_facts(mounted):
    agent, proc, _ = mounted
    with (proc / "42" / "cmdline").open("ab") as stream:
        stream.write(b"--unused=\xff\0")
    assert live_binding.compare(agent, live_binding.read(agent, 42, proc)) == {}


def test_legacy_assignment_uses_the_profile_home_and_saved_fields(tmp_path, monkeypatch):
    from pathlib import Path

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    agent = AgentRecord(
        "old", "eng", "one", harness="codex", profile="engineer", model="gpt-6.1-sol", effort="high", account="default"
    )
    assert live_binding.assignment(agent) == {
        "home": str(tmp_path / ".agentihooks" / "profiles" / "engineer" / "codex"),
        "harness": "codex",
        "profile": "engineer",
        "model": "gpt-6.1-sol",
        "effort": "high",
        "account": "default",
    }


def test_mismatch_report_and_health_evidence_are_complete(ticking):
    store, _, _ = ticking
    agent = AgentRecord("engineer", "eng", "one", harness="claude", profile="engineer")
    differences = live_binding.record(store, "sw", agent, {"pane": "open"}, 123)
    assert differences == {"pane": {"expected": "closed", "actual": "open"}}
    assert json.loads(store.redis.hget(store.key("sw", "live-bindings"), "engineer")) == {
        "agent": "engineer",
        "at": 123,
        "state": "mismatched",
        "differences": differences,
    }
    found = live_binding.findings(store, "sw")
    assert len(found) == 1
    assert found[0].as_dict() == {
        "kind": "live binding",
        "subject": "engineer/pane",
        "summary": "engineer differs in pane",
        "evidence": ["expected closed; observed open"],
        "threshold": "assigned launch must match",
    }
    assert found[0].measure == 1


def test_relaunch_never_reclassifies_the_assigned_profile(tmp_path, monkeypatch):
    from scripts.swarm import profile_choice
    from tests.swarm.test_runtime import _launched

    def refuse(*args):
        pytest.fail("the saved assignment must not be classified")

    monkeypatch.setattr(profile_choice, "choose", refuse)
    saved = {"profile": "engineer", "harness": "claude", "model": "opus", "effort": "high", "account": "team"}
    _launched(
        tmp_path, monkeypatch, "eng", {"id": "one", "title": "Verify", "profile": "qa", "launch_assignment": saved}
    )


@pytest.mark.parametrize("fields", [{"state": "done"}, {"state": "blocked"}, {"state": "handoff"}, {"done": True}])
def test_ended_task_closes_its_pane_on_the_same_tick_without_a_pane_finding(ticking, fields):
    from scripts.swarm.tick import tick

    store, runtime, ledger = ticking
    tick("sw", store, ledger, runtime, 100)
    old = next(a for a in store.agents("sw") if a.lane == "eng")
    ledger.rows["one"].update(fields)
    tick("sw", store, ledger, runtime, 200)
    assert old.pane_id in runtime.closed
    assert store.redis.hget(store.key("sw", "live-bindings"), old.name) is None
    assert live_binding.findings(store, "sw") == []
    assert runtime.tasks[-1].get("launch_assignment") is None


def test_finished_agent_on_open_task_closes_its_pane_without_a_pane_finding(ticking):
    from scripts.swarm.tick import tick

    store, runtime, ledger = ticking
    tick("sw", store, ledger, runtime, 100)
    old = next(a for a in store.agents("sw") if a.lane == "eng")
    store.put_agent("sw", replace(old, state="finished"))
    tick("sw", store, ledger, runtime, 200)
    assert old.pane_id in runtime.closed
    assert store.redis.hget(store.key("sw", "live-bindings"), old.name) is None


def test_pending_relaunch_assignment_is_cleared_after_worker_launch(ticking):
    from scripts.swarm.tick import tick

    store, runtime, ledger = ticking
    saved = {
        "profile": "engineer",
        "harness": "claude",
        "model": "opus",
        "effort": "high",
        "account": "team",
        "seat": "eng-2@sw",
    }
    store.redis.hset(store.key("sw", "launch-assignments"), "one", json.dumps(saved))
    tick("sw", store, ledger, runtime, 100)
    assert next(a for a in store.agents("sw") if a.lane == "eng").seat == "eng-2@sw"
    assert runtime.tasks[-1]["launch_assignment"] == saved
    assert store.redis.hget(store.key("sw", "launch-assignments"), "one") is None


def test_pending_master_relaunch_assignment_is_cleared_after_launch(ticking):
    from scripts.swarm.tick import tick

    store, runtime, ledger = ticking
    saved = {"profile": "master", "harness": "claude", "model": "opus", "effort": "high", "account": "team"}
    store.redis.hset(store.key("sw", "launch-assignments"), "master", json.dumps(saved))
    tick("sw", store, ledger, runtime, 100)
    assert runtime.masters[-1][1]["launch_assignment"] == saved
    assert store.redis.hget(store.key("sw", "launch-assignments"), "master") is None


def test_awaiting_decision_agent_is_skipped_without_skipping_its_peer(ticking):
    from scripts.swarm.tick import tick

    store, runtime, ledger = ticking
    tick("sw", store, ledger, runtime, 100)
    old = next(a for a in store.agents("sw") if a.lane == "eng")
    store.put_agent("sw", replace(old, state="awaiting-decision"))
    runtime.bindings = lambda agents: {a.name: {"process": False} for a in agents}
    tick("sw", store, ledger, runtime, 200)
    assert any(a.name == old.name for a in store.agents("sw"))
    assert old.name not in runtime.killed
    assert any(name.startswith("master") for name in runtime.killed)


def test_one_finished_or_missing_process_does_not_skip_later_agents(ticking):
    from scripts.swarm.tick import tick

    store, runtime, ledger = ticking
    tick("sw", store, ledger, runtime, 100)
    old = next(a for a in store.agents("sw") if a.lane == "eng")
    ledger.rows["one"].update(state="done", done=True)
    actions = tick("sw", store, ledger, runtime, 200)
    assert old.pane_id in runtime.closed
    master = next(a for a in store.agents("sw") if a.lane == "master")
    assert json.loads(store.redis.hget(store.key("sw", "live-bindings"), master.name))["at"] == 200
    assert all(action is not None for action in actions)


def test_closed_finished_pane_does_not_create_an_open_pane_finding(ticking):
    from scripts.swarm.tick import tick

    store, runtime, ledger = ticking
    tick("sw", store, ledger, runtime, 100)
    old = next(a for a in store.agents("sw") if a.lane == "eng")
    ledger.rows["one"].update(state="done", done=True)
    runtime.closed.append(old.pane_id)
    tick("sw", store, ledger, runtime, 200)
    assert not any(f.subject == f"{old.name}/pane" for f in live_binding.findings(store, "sw"))


def test_multiple_mismatches_are_reported_in_order(ticking):
    from scripts.swarm.tick import tick

    store, runtime, ledger = ticking
    tick("sw", store, ledger, runtime, 100)
    old = next(a for a in store.agents("sw") if a.lane == "eng")
    runtime.stuck.add(old.name)
    runtime.bindings = lambda agents: {
        a.name: {
            **live_binding.assignment(a),
            "hooks": True,
            **({"model": "wrong", "effort": "wrong"} if a.name == old.name else {}),
        }
        for a in agents
    }
    actions = tick("sw", store, ledger, runtime, 200)
    assert actions == [f"could not retire {old.name} after mismatched model, effort, retrying next tick"]


def test_recovered_registration_restores_working_state(ticking):
    from scripts.swarm.tick import tick

    store, runtime, ledger = ticking
    tick("sw", store, ledger, runtime, 100)
    old = next(a for a in store.agents("sw") if a.lane == "eng")
    store.put_agent("sw", replace(old, state="retiring"))
    tick("sw", store, ledger, runtime, 200)
    assert next(a for a in store.agents("sw") if a.name == old.name).state == "working"
    assert not runtime.killed


def test_missing_process_retry_has_named_evidence_and_checks_other_agents(ticking):
    from scripts.swarm.tick import tick

    store, runtime, ledger = ticking
    tick("sw", store, ledger, runtime, 100)
    old = next(a for a in store.agents("sw") if a.lane == "eng")
    store.put_agent("sw", replace(old, state="retiring"))
    runtime.stuck.add(old.name)
    runtime.live.remove(old.name)
    runtime.bindings = lambda agents: {
        a.name: {**live_binding.assignment(a), "hooks": True} for a in agents if a.name in runtime.live
    }
    tick("sw", store, ledger, runtime, 200)
    report = json.loads(store.redis.hget(store.key("sw", "live-bindings"), old.name))
    assert report["differences"] == {"process": {"expected": True, "actual": False}}
    master = next(a for a in store.agents("sw") if a.lane == "master")
    assert json.loads(store.redis.hget(store.key("sw", "live-bindings"), master.name))["at"] == 200
    assert master.name in runtime.named


def test_missing_unbound_agent_does_not_skip_the_live_master(ticking):
    from scripts.swarm.tick import tick

    store, runtime, ledger = ticking
    tick("sw", store, ledger, runtime, 100)
    old = next(a for a in store.agents("sw") if a.lane == "eng")
    runtime.live.remove(old.name)
    runtime.bindings = lambda agents: {
        a.name: {**live_binding.assignment(a), "hooks": True} for a in agents if a.name in runtime.live
    }
    tick("sw", store, ledger, runtime, 200)
    master = next(a for a in store.agents("sw") if a.lane == "master")
    assert json.loads(store.redis.hget(store.key("sw", "live-bindings"), master.name))["at"] == 200


def test_python_lifecycle_accepts_hook_module_arguments(mounted):
    agent, proc, home = mounted
    data = json.loads((home / "settings.json").read_text())
    data["hooks"]["SessionStart"][0]["hooks"][0]["command"] = "python3 -m hooks argument"
    (home / "settings.json").write_text(json.dumps(data))
    assert live_binding.read(agent, 42, proc)["hooks"] is True


def test_cd_preamble_can_invoke_the_registered_wrapper(mounted):
    agent, proc, home = mounted
    wrapper = home / "agentihooks-hook.sh"
    wrapper.touch()
    wrapper.chmod(0o700)
    data = json.loads((home / "settings.json").read_text())
    data["hooks"]["SessionStart"][0]["hooks"][0]["command"] = f"cd {home} && {wrapper}"
    (home / "settings.json").write_text(json.dumps(data))
    assert live_binding.read(agent, 42, proc)["hooks"] is True


@pytest.mark.parametrize(
    "validated_pid, conversation, expected",
    [
        (22, "", 22),
        (99, "", None),
        (None, "current", 22),
        (None, "", 11),
    ],
)
def test_named_proof_process_cannot_shadow_assigned_session(monkeypatch, validated_pid, conversation, expected):
    from types import SimpleNamespace

    from scripts import terminate_agent
    from scripts.swarm.runtime import HerdrRuntime

    def item(pid, target, status, session_id, started):
        return SimpleNamespace(
            name="engineer",
            target=target,
            status=status,
            session_id=session_id,
            process=SimpleNamespace(pid=pid, start_time=started),
        )

    items = [
        item(33, "codex", "unregistered", "proof", 1),
        item(22, "claude", "alive", "current", 3),
        item(11, "claude", "alive", "original", 2),
    ]
    monkeypatch.setattr(terminate_agent, "sessions", lambda: items)
    validation = {"pid": validated_pid} if validated_pid is not None else {}
    agent = AgentRecord(
        "engineer",
        "eng",
        "one",
        harness="claude",
        conversation_id=conversation,
        profile_decision={"validation": validation},
    )
    monkeypatch.setattr(live_binding, "read", lambda agent, pid: {"pid": pid})
    runtime = HerdrRuntime()
    assert runtime.bindings([agent]) == {"engineer": {"pid": expected} if expected else {"process": False}}


def test_retirement_uses_the_same_process_the_verifier_checked(monkeypatch):
    from types import SimpleNamespace

    from scripts import terminate_agent
    from scripts.swarm.runtime import HerdrRuntime

    monkeypatch.setattr(
        terminate_agent,
        "sessions",
        lambda: [
            SimpleNamespace(
                name="engineer",
                target="claude",
                status="alive",
                session_id="",
                process=SimpleNamespace(pid=11, start_time=1),
            ),
            SimpleNamespace(
                name="engineer",
                target="codex",
                status="unregistered",
                session_id="",
                process=SimpleNamespace(pid=22, start_time=2),
            ),
        ],
    )
    monkeypatch.setattr(live_binding, "read", lambda agent, pid: {"hooks": False})
    from scripts.swarm.reaper import Outcome

    ended = []
    agent = AgentRecord("engineer", "eng", "one", harness="claude")
    runtime = HerdrRuntime(run=lambda argv, **kwargs: pytest.fail("retire never runs terminate-agent"))
    runtime.end = lambda name, pid, homes, start=0: ended.append((name, pid, homes)) or Outcome((pid,))
    runtime.bindings([agent])
    assert runtime.retire(agent) is True
    assert ended == [("engineer", "11", [])]


def test_missing_validated_process_closes_without_terminating_foreign_session(monkeypatch):
    from scripts import terminate_agent
    from scripts.swarm.runtime import HerdrRuntime

    monkeypatch.setattr(terminate_agent, "sessions", lambda: [])

    def refuse(*args, **kwargs):
        pytest.fail("a missing assigned process must not select another process")

    runtime = HerdrRuntime(run=refuse)
    agent = AgentRecord("engineer", "eng", "one", profile_decision={"validation": {"pid": 99}})
    assert runtime.bindings([agent]) == {"engineer": {"process": False}}
    assert runtime.retire(agent) is True


def test_unbound_absent_process_has_no_live_report(monkeypatch):
    from scripts import terminate_agent
    from scripts.swarm.runtime import HerdrRuntime

    monkeypatch.setattr(terminate_agent, "sessions", lambda: [])
    assert HerdrRuntime().bindings([AgentRecord("engineer", "eng", "one")]) == {}


@pytest.mark.parametrize(
    "status, session_id, expected",
    [
        ("alive", "current", {"pid": 22, "rebound": 22}),
        ("unregistered", "current", {"process": False}),
        ("alive", "other", {"process": False}),
    ],
)
def test_a_session_resumed_under_a_new_process_rebinds_by_name_and_conversation(
    monkeypatch, status, session_id, expected
):
    from types import SimpleNamespace

    from scripts import terminate_agent
    from scripts.swarm.runtime import HerdrRuntime

    resumed = SimpleNamespace(
        name="engineer",
        target="claude",
        status=status,
        session_id=session_id,
        process=SimpleNamespace(pid=22, start_time=3),
    )
    monkeypatch.setattr(terminate_agent, "sessions", lambda: [resumed])
    monkeypatch.setattr(live_binding, "read", lambda agent, pid: {"pid": pid})
    agent = AgentRecord(
        "engineer",
        "eng",
        "one",
        harness="claude",
        conversation_id="current",
        profile_decision={"validation": {"pid": 99}},
    )
    assert HerdrRuntime().bindings([agent]) == {"engineer": expected}


@pytest.mark.parametrize(
    "validation, read, expected",
    [
        ({"pid": 22}, {"pid": 22}, {"pid": 22}),
        ({}, {"pid": 22}, {"pid": 22}),
        ({"pid": 99}, {"process": False}, {"process": False}),
    ],
)
def test_bindings_report_a_rebound_only_for_a_live_process_other_than_the_validated_one(
    monkeypatch, validation, read, expected
):
    from types import SimpleNamespace

    from scripts import terminate_agent
    from scripts.swarm.runtime import HerdrRuntime

    session = SimpleNamespace(
        name="engineer",
        target="claude",
        status="alive",
        session_id="current",
        process=SimpleNamespace(pid=22, start_time=3),
    )
    monkeypatch.setattr(terminate_agent, "sessions", lambda: [session])
    monkeypatch.setattr(live_binding, "read", lambda agent, pid: dict(read))
    agent = AgentRecord(
        "engineer",
        "eng",
        "one",
        harness="claude",
        conversation_id="current",
        profile_decision={"validation": validation},
    )
    assert HerdrRuntime().bindings([agent]) == {"engineer": expected}


@pytest.mark.parametrize("panes", [{}, {"w1:p9": "conv", "w1:p8": "conv"}, {"w1:p9": "other"}])
def test_tick_holds_a_resumed_agent_until_one_pane_holds_its_conversation(ticking, panes):
    from scripts.swarm.tick import tick

    store, runtime, ledger = ticking
    tick("sw", store, ledger, runtime, 100)
    old = next(a for a in store.agents("sw") if a.lane == "eng")
    old = replace(old, conversation_id="conv", profile_decision={"validation": {"pid": 99}})
    twin = replace(old, name=f"{old.name}-twin")
    store.put_agent("sw", old)
    store.put_agent("sw", twin)
    runtime.conversation_ids = panes
    runtime.bindings = lambda agents: {
        a.name: {**live_binding.assignment(a), "hooks": True, **({"rebound": 1234} if a.lane == "eng" else {})}
        for a in agents
    }
    actions = tick("sw", store, ledger, runtime, 200)
    now = next(a for a in store.agents("sw") if a.name == old.name)
    assert now.profile_decision["validation"]["pid"] == 99
    assert now.pane_id == old.pane_id
    assert not runtime.killed
    assert {f"held {a.name} until one pane holds its resumed process 1234" for a in (old, twin)} <= set(actions)


@pytest.mark.parametrize(
    "decision, expected",
    [
        ({"profile": "engineer"}, {"profile": "engineer", "validation": {"pid": 7}}),
        ({"validation": {"pid": 1, "canary": "c"}}, {"validation": {"pid": 7, "canary": "c"}}),
    ],
)
def test_rebind_keeps_the_launch_decision_and_moves_only_the_validated_process(ticking, decision, expected):
    from scripts.swarm.tick import _rebind

    store, runtime, _ = ticking
    runtime.conversation_ids = {"w1:p9": "conv", "w1:p8": "other"}
    agent = AgentRecord("engineer", "eng", "one", conversation_id="conv", profile_decision=decision)
    rebound = _rebind("sw", store, runtime, agent, 7)
    assert (rebound.pane_id, rebound.profile_decision) == ("w1:p9", expected)


def test_a_validated_agent_rebinds_to_its_earliest_resumed_session():
    from types import SimpleNamespace

    def session(pid, start):
        return SimpleNamespace(
            name="engineer",
            target="claude",
            status="alive",
            session_id="conv",
            process=SimpleNamespace(pid=pid, start_time=start),
        )

    agent = AgentRecord("engineer", "eng", "one", conversation_id="conv", profile_decision={"validation": {"pid": 99}})
    assert live_binding.bound_session(agent, [session(31, 9), session(32, 4), session(33, 6)]).process.pid == 32


def test_tick_keeps_a_resumed_agent_and_records_its_new_process_and_pane(ticking):
    from scripts.swarm.tick import tick

    store, runtime, ledger = ticking
    tick("sw", store, ledger, runtime, 100)
    old = next(a for a in store.agents("sw") if a.lane == "eng")
    old = replace(
        old, conversation_id="conv", profile_decision={"profile": "engineer", "validation": {"pid": 99, "canary": "c"}}
    )
    store.put_agent("sw", old)
    runtime.conversation_ids = {"w1:p9": "conv", "w1:p8": "other"}
    runtime.bindings = lambda agents: {
        a.name: {**live_binding.assignment(a), "hooks": True, **({"rebound": 1234} if a.name == old.name else {})}
        for a in agents
    }
    actions = tick("sw", store, ledger, runtime, 200)
    now = next(a for a in store.agents("sw") if a.name == old.name)
    assert now.profile_decision == {"profile": "engineer", "validation": {"pid": 1234, "canary": "c"}}
    assert now.pane_id == "w1:p9"
    assert not runtime.killed
    assert ledger.rows["one"]["claimed_by"] == old.name
    assert f"rebound {old.name} to its resumed process 1234 in pane w1:p9" in actions


@pytest.mark.parametrize("proof_harness, proof_status", [("claude", "unregistered"), ("codex", "alive")])
def test_legacy_reader_prefers_registered_assigned_harness(monkeypatch, proof_harness, proof_status):
    from types import SimpleNamespace

    from scripts import terminate_agent
    from scripts.swarm.runtime import HerdrRuntime

    monkeypatch.setattr(
        terminate_agent,
        "sessions",
        lambda: [
            SimpleNamespace(
                name="engineer",
                target=proof_harness,
                status=proof_status,
                session_id="",
                process=SimpleNamespace(pid=11, start_time=1),
            ),
            SimpleNamespace(
                name="engineer",
                target="claude",
                status="alive",
                session_id="",
                process=SimpleNamespace(pid=22, start_time=2),
            ),
        ],
    )
    monkeypatch.setattr(live_binding, "read", lambda agent, pid: {"pid": pid})
    agent = AgentRecord("engineer", "eng", "one", harness="claude")
    assert HerdrRuntime().bindings([agent]) == {"engineer": {"pid": 22}}


@pytest.mark.parametrize(
    "lanes, expected",
    [({"master": {"profile": "master"}}, "master"), ({}, "master"), ({"master": {"profile": "qa"}}, "qa")],
)
def test_legacy_master_relaunch_uses_its_declared_profile(ticking, monkeypatch, lanes, expected):
    from scripts.swarm.tick import tick

    monkeypatch.setenv("AGENTIHOOKS_MASTER_RETIRE_HANDOFF_MINUTES", "0")
    store, runtime, ledger = ticking
    store.update("sw", lanes=lanes)
    tick("sw", store, ledger, runtime, 100)
    old = next(a for a in store.agents("sw") if a.lane == "master")
    store.put_agent("sw", replace(old, model="opus", effort="high"))
    runtime.bindings = lambda agents: {
        a.name: {**live_binding.assignment(a), "hooks": a.name != old.name, "profile": "master"} for a in agents
    }
    tick("sw", store, ledger, runtime, 200)
    saved = runtime.masters[-1][1]["launch_assignment"]
    assert saved["profile"] == expected
    assert saved["model"] == "opus"
    assert saved["effort"] == "high"


def test_legacy_worker_relaunch_prefers_explicit_task_profile(ticking):
    from scripts.swarm.tick import tick

    store, runtime, ledger = ticking
    ledger.rows["one"]["profile"] = "qa"
    store.update("sw", lanes={"eng": {"profile": "engineer"}})
    tick("sw", store, ledger, runtime, 100)
    old = next(a for a in store.agents("sw") if a.lane == "eng")
    runtime.bindings = lambda agents: {
        a.name: {**live_binding.assignment(a), "hooks": a.name != old.name} for a in agents
    }
    tick("sw", store, ledger, runtime, 200)
    assert runtime.tasks[-1]["launch_assignment"]["profile"] == "qa"


def test_adopted_master_with_an_empty_record_takes_its_live_binding(ticking):
    from pathlib import Path

    from scripts.swarm.tick import tick

    store, runtime, ledger = ticking
    tick("sw", store, ledger, runtime, 100)
    master = next(a for a in store.agents("sw") if a.lane == "master")
    store.put_agent("sw", replace(master, harness="", profile="", model="", effort="", account=""))
    live = {
        "harness": "claude",
        "home": str((Path.home() / ".agentihooks" / "profiles" / "master" / "claude").resolve()),
        "profile": "master",
        "model": "opus",
        "effort": "high",
        "account": "team",
        "hooks": True,
    }
    runtime.bindings = lambda agents: {
        a.name: live if a.name == master.name else {**live_binding.assignment(a), "hooks": True} for a in agents
    }
    tick("sw", store, ledger, runtime, 200)
    kept = next(a for a in store.agents("sw") if a.name == master.name)
    assert master.name not in runtime.killed
    assert (kept.harness, kept.profile, kept.model, kept.effort, kept.account) == (
        "claude",
        "master",
        "opus",
        "high",
        "team",
    )
    assert json.loads(store.redis.hget(store.key("sw", "live-bindings"), master.name))["state"] == "matching"


def test_master_without_a_complete_relaunch_assignment_is_kept(ticking):
    from scripts.swarm.tick import tick

    store, runtime, ledger = ticking
    tick("sw", store, ledger, runtime, 100)
    master = next(a for a in store.agents("sw") if a.lane == "master")
    store.put_agent("sw", replace(master, harness="", profile="", model="", effort="", account=""))
    runtime.bindings = lambda agents: {
        a.name: {**live_binding.assignment(a), "hooks": a.name != master.name} for a in agents
    }
    actions = tick("sw", store, ledger, runtime, 200)
    assert master.name not in runtime.killed
    assert any(a.name == master.name for a in store.agents("sw"))
    assert f"kept {master.name} after mismatched hooks: its relaunch assignment is incomplete" in actions
    assert store.redis.hget(store.key("sw", "launch-assignments"), "master") is None


def test_fill_takes_only_the_fields_the_record_lacks():
    agent = AgentRecord("master", "master", "master", account="team")
    filled = live_binding.fill(agent, {"harness": "claude", "profile": "master", "account": "other"})
    assert (filled.harness, filled.profile, filled.model, filled.account) == ("claude", "master", "", "team")
    bound = replace(agent, harness="codex")
    assert live_binding.fill(bound, {"harness": "claude", "model": "opus"}) is bound


def test_retiring_unbound_master_without_a_live_process_is_checked(ticking):
    from scripts.swarm.tick import tick

    store, runtime, ledger = ticking
    tick("sw", store, ledger, runtime, 100)
    master = next(a for a in store.agents("sw") if a.lane == "master")
    store.put_agent("sw", replace(master, harness="", state="retiring"))
    runtime.bindings = lambda agents: {
        a.name: {**live_binding.assignment(a), "hooks": True} for a in agents if a.name != master.name
    }
    tick("sw", store, ledger, runtime, 200)
    report = json.loads(store.redis.hget(store.key("sw", "live-bindings"), master.name))
    assert report["differences"] == {"process": {"expected": True, "actual": False}}


def test_a_kept_master_does_not_skip_later_agents(ticking):
    from scripts.swarm.tick import tick

    store, runtime, ledger = ticking
    tick("sw", store, ledger, runtime, 100)
    master = next(a for a in store.agents("sw") if a.lane == "master")
    store.put_agent("sw", replace(master, harness="", profile="", model="", effort="", account=""))
    later = AgentRecord("worker@zz", "eng", "two", harness="claude", profile="engineer", model="opus", effort="high")
    store.put_agent("sw", later)
    runtime.bindings = lambda agents: {
        a.name: {**live_binding.assignment(a), "hooks": a.name != master.name} for a in agents
    }
    tick("sw", store, ledger, runtime, 200)
    assert json.loads(store.redis.hget(store.key("sw", "live-bindings"), later.name))["at"] == 200
