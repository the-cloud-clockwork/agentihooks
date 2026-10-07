import json

import pytest

from scripts.profiles import binding


@pytest.mark.parametrize("target", ["claude", "codex"])
def test_rendered_binding_rejects_wrong_profile_and_missing_home(tmp_path, target):
    home = tmp_path / "engineer" / target
    home.mkdir(parents=True)
    persona = home / binding.PERSONAS[target]
    persona.write_text("Use the engineer persona.\n")
    manifest = home.parent / f"{target}.sources.json"
    manifest.write_text(json.dumps([]))
    binding.write(home, "engineer", target)
    assert binding.inspect(home, "engineer", target)["profile"] == "engineer"
    with pytest.raises(ValueError, match="profile mismatch"):
        binding.inspect(home, "qa", target)
    with pytest.raises(ValueError, match="missing profile home"):
        binding.inspect(tmp_path / "missing", "engineer", target)
    persona.write_text("Use the qa persona.\n")
    with pytest.raises(ValueError, match="persona changed"):
        binding.inspect(home, "engineer", target)


@pytest.mark.parametrize("target", ["claude", "codex"])
def test_canary_requires_real_harness_home_and_mounted_instruction(tmp_path, monkeypatch, target):
    home = tmp_path / "qa" / target
    home.mkdir(parents=True)
    (home / binding.PERSONAS[target]).write_text(binding.persona("Use the qa persona.\n"))
    (home.parent / f"{target}.sources.json").write_text("[]")
    binding.write(home, "qa", target)
    report = tmp_path / "report.json"
    binding.request(report, "qa", target)
    env = {"AGENTIHOOKS_PROFILE": "qa", binding.HOMES[target]: str(home), binding.REPORT: str(report)}
    monkeypatch.setattr(binding, "process", lambda: (123, target, env, "account-one"))
    mounted = binding.inspect(home, "qa", target)
    with pytest.raises(ValueError, match="canary mismatch"):
        binding.validate("invented")
    result = binding.validate(mounted["canary"])
    assert result["state"] == "validated"
    assert result["pid"] == 123
    assert result["account"] == "account-one"
    assert json.loads(report.read_text())["validation"] == result
    env[binding.HOMES[target]] = str(tmp_path / "missing")
    with pytest.raises(ValueError, match="missing profile home"):
        binding.validate(mounted["canary"])


def test_quota_transfer_from_codex_reports_unsupported_before_launch(tmp_path, monkeypatch, capsys):
    from scripts import init_agent

    monkeypatch.setattr(init_agent, "_write_launcher", lambda *a: pytest.fail("unsupported transfer launched"))
    result = init_agent.main(
        ["--handoff", "--dir", str(tmp_path), "--prompt", "continue"],
        {"AGENTIHOOKS_TARGET": "codex"},
    )
    assert result == 2
    assert "unsupported" in capsys.readouterr().err


@pytest.mark.parametrize("target", ["claude", "codex"])
@pytest.mark.parametrize("control", ["valid", "wrong", "missing", "absent"])
def test_launch_success_requires_live_binding_canary(tmp_path, monkeypatch, capsys, target, control):
    from scripts import init_agent, select_profile

    home = tmp_path / "engineer" / target
    home.mkdir(parents=True)
    (home / binding.PERSONAS[target]).write_text(binding.persona("Engineer instructions.\n"))
    (home.parent / f"{target}.sources.json").write_text("[]")
    binding.write(home, "engineer", target)
    rendered = binding.inspect(home, "engineer", target)
    monkeypatch.setattr(
        select_profile,
        "prepare",
        lambda *a: ({"AGENTIHOOKS_PROFILE": "engineer", binding.HOMES[target]: str(home)}, []),
    )
    monkeypatch.setattr(init_agent.agent_choice, "choose", lambda *a: (target, "explicit"))
    monkeypatch.setattr(init_agent.claude_trust, "ensure_trusted", lambda *a: ("trusted", ""))
    monkeypatch.setattr(init_agent.herdr_host, "rename_agent", lambda *a: True)

    def launch(launcher, directory, name, args, agent, environ):
        init_agent._started_marker(launcher).touch()
        init_agent._route_report(launcher).write_text('{"status":"direct"}')
        env = dict(environ)
        if control == "wrong":
            env["AGENTIHOOKS_PROFILE"] = "qa"
        if control == "missing":
            env[binding.HOMES[target]] = str(tmp_path / "missing")
        monkeypatch.setattr(binding, "process", lambda: (123, target, env, "test-account"))
        if control != "absent":
            try:
                binding.validate(rendered["canary"])
            except ValueError:
                pass
        return []

    monkeypatch.setattr(init_agent, "_start_herdr", launch)
    args = [
        "--host",
        "herdr",
        "--agent",
        target,
        "--profile",
        "engineer",
        "--dir",
        str(tmp_path),
        "--route-timeout",
        "0.1",
    ]
    result = init_agent.main(args, {"XDG_RUNTIME_DIR": str(tmp_path / "runtime")})
    output = capsys.readouterr()
    if control == "valid":
        assert result == 0
        assert "profile_validation=validated" in output.out
        assert '"pid":123' in output.out
    else:
        assert result == 3
        assert "profile_validation=failed" in output.out


def test_swarm_refuses_a_started_process_without_validation(tmp_path):
    from types import SimpleNamespace

    from scripts.swarm.runtime import HerdrRuntime
    from scripts.swarm.tick import SpawnError

    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="status=started\nroute_status=routed\n", stderr="")

    runtime = HerdrRuntime(home=tmp_path, run=run)
    config = SimpleNamespace(slug="proof", repo=str(tmp_path), autonomy="delegate", compact_limit=0)
    with pytest.raises(SpawnError, match="validation is missing"):
        runtime._launch(config, "eng", "task", "worker", ["init-agent", "--agent", "codex", "--profile", "engineer"])
    assert "terminate-agent" in calls[-1]


def test_supported_quota_transfer_preserves_resolved_run_options(monkeypatch):
    env = {"AGENTIHOOKS_RUN_MODEL": "sonnet", "AGENTIHOOKS_RUN_EFFORT": "medium"}
    monkeypatch.setattr(binding, "process", lambda: (123, "claude", env, "original"))
    assert binding.continuation(["--route", "other"], "claude") == [
        "--model",
        "sonnet",
        "--effort",
        "medium",
        "--route",
        "other",
    ]
    with pytest.raises(ValueError, match="unsupported quota transfer"):
        binding.continuation([], "codex")


def test_swarm_handoff_keeps_profile_harness_model_effort_and_account(tmp_path):
    from types import SimpleNamespace

    from scripts.swarm.runtime import HerdrRuntime
    from tests.swarm.profile_fixture import validated

    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        out = "status=started\nroute_status=routed\nmodel=saved-model\neffort=medium\naccount=original\n"
        return SimpleNamespace(returncode=0, stdout=validated(argv, out), stderr="")

    runtime = HerdrRuntime(home=tmp_path, run=run, choose=lambda requested, env: (requested or "claude", "pinned"))
    config = SimpleNamespace(
        slug="proof",
        repo=str(tmp_path),
        code="a1b2c3",
        lanes={"eng": {"agent": "claude", "model": "opus", "effort": "high"}},
        autonomy="delegate",
        compact_limit=0,
    )
    saved = {"harness": "codex", "profile": "qa", "model": "saved-model", "effort": "medium", "account": "original"}
    task = {
        "id": "task",
        "title": "Independent proof",
        "profile": "qa",
        "handoff": "continue",
        "handoff_envelope": {"launch": saved},
    }
    placed = runtime.spawn(config, "eng", "worker", task)
    argv = calls[0]
    assert argv[argv.index("--agent") + 1] == "codex"
    assert argv[argv.index("--profile") + 1] == "qa"
    assert argv[argv.index("--") + 1 :] == [
        "--route",
        "original",
        "-m",
        "saved-model",
        "-c",
        'model_reasoning_effort="medium"',
    ]
    assert placed.profile_decision["validation"]["harness"] == "codex"


def test_resume_refuses_a_missing_original_profile(tmp_path):
    from dataclasses import replace

    from scripts.swarm.tick import SpawnError
    from tests.swarm.test_runtime import _resuming

    runtime, config, agent, seen = _resuming(tmp_path, "c0ffee")
    with pytest.raises(SpawnError, match="original profile"):
        runtime.resume(config, replace(agent, profile=""), "continue")
    assert not seen["runs"]


@pytest.mark.parametrize(
    "target,model,native,account_name",
    [
        ("claude", "opus", ["--model", "opus", "--effort", "medium"], "AH_CC_TOKEN_fixture"),
        ("codex", "gpt-6.1-sol", ["-m", "gpt-6.1-sol", "-c", 'model_reasoning_effort="medium"'], "AH_CX_TOKEN_fixture"),
    ],
)
def test_process_binding_reads_only_allowed_fields_from_real_ancestor_shape(
    tmp_path, target, model, native, account_name
):
    shell, agent = tmp_path / "3", tmp_path / "2"
    shell.mkdir()
    agent.mkdir()
    (shell / "comm").write_text("bash\n")
    (shell / "status").write_text("Name: bash\nPPid: 2\n")
    (agent / "comm").write_text(target + "\n")
    items = [
        account_name,
        "IGNORED_DATA=not-exported",
        "AGENTIHOOKS_PROFILE=qa",
        "AGENTIHOOKS_PROFILE_REPORT=/proof/request",
        f"{binding.HOMES[target]}=/proof/home",
    ]
    (agent / "environ").write_bytes("\0".join(items).encode())
    (agent / "cmdline").write_bytes("\0".join([target, *native]).encode())
    pid, harness, env, account = binding.process(tmp_path, 3)
    assert (pid, harness, account) == (2, target, "fixture")
    assert env == {
        "AGENTIHOOKS_PROFILE": "qa",
        "AGENTIHOOKS_PROFILE_REPORT": "/proof/request",
        binding.HOMES[target]: "/proof/home",
        "AGENTIHOOKS_RUN_MODEL": model,
        "AGENTIHOOKS_RUN_EFFORT": "medium",
    }
    with pytest.raises(ValueError, match="no supported live harness"):
        binding.process(tmp_path, 1)
    with pytest.raises(ValueError, match="unsupported live process"):
        binding.process(tmp_path / "absent", 3)
