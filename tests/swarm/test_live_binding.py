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

    def partial(agent, live):
        if agent.name == old.name:
            runtime.live.discard(old.name)
            return False
        return retire(agent, live)

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


@pytest.mark.parametrize(
    "found, expected",
    [
        ({"pane_id": "pane", "name": "engineer"}, True),
        ({"pane_id": "foreign", "name": "foreign"}, False),
        (None, False),
    ],
)
def test_finished_pane_lookup_checks_owned_pane(found, expected):
    from scripts.swarm.runtime import HerdrRuntime

    calls = []

    def herdr(argv):
        calls.append(argv)
        if found is None:
            raise RuntimeError("not found")
        return {"agent": found}

    agent = AgentRecord("engineer", "eng", "one", pane_id="pane")
    assert HerdrRuntime(herdr=herdr).pane_open(agent) is expected
    assert calls == [["agent", "get", "pane"]]


@pytest.mark.parametrize("fields", [{"state": "done"}, {"state": "blocked"}, {"state": "handoff"}, {"done": True}])
def test_ended_task_pane_is_reported_without_relaunch_assignment(ticking, fields):
    from scripts.swarm.tick import tick

    store, runtime, ledger = ticking
    tick("sw", store, ledger, runtime, 100)
    old = next(a for a in store.agents("sw") if a.lane == "eng")
    ledger.rows["one"].update(fields)
    tick("sw", store, ledger, runtime, 200)
    report = json.loads(store.redis.hget(store.key("sw", "live-bindings"), old.name))
    assert report == {
        "agent": old.name,
        "at": 200,
        "state": "mismatched",
        "differences": {"pane": {"expected": "closed", "actual": "open"}},
    }
    assert runtime.tasks[-1].get("launch_assignment") is None


def test_finished_agent_on_open_task_closes_its_pane(ticking):
    from scripts.swarm.tick import tick

    store, runtime, ledger = ticking
    tick("sw", store, ledger, runtime, 100)
    old = next(a for a in store.agents("sw") if a.lane == "eng")
    store.put_agent("sw", replace(old, state="finished"))
    tick("sw", store, ledger, runtime, 200)
    assert old.pane_id in runtime.closed
    report = json.loads(store.redis.hget(store.key("sw", "live-bindings"), old.name))
    assert report["differences"] == {"pane": {"expected": "closed", "actual": "open"}}


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
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0)

    agent = AgentRecord("engineer", "eng", "one", harness="claude")
    runtime = HerdrRuntime(run=run)
    runtime.bindings([agent])
    assert runtime.retire(agent, True) is True
    assert calls[0][1:] == ["terminate-agent", "11", "--force-shared"]
