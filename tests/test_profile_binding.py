import json
from pathlib import Path

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


@pytest.mark.parametrize("target", ["claude", "codex"])
@pytest.mark.parametrize(
    "field,value", [("pid", 456), ("profile", "qa"), ("harness", "other"), ("home", "/other"), ("state", "pending")]
)
def test_revalidation_refuses_a_different_live_binding(tmp_path, monkeypatch, target, field, value):
    home = tmp_path / "engineer" / target
    home.mkdir(parents=True)
    (home / binding.PERSONAS[target]).write_text(binding.persona("Engineer instructions.\n"))
    (home.parent / f"{target}.sources.json").write_text("[]")
    binding.write(home, "engineer", target)
    report = tmp_path / "report.json"
    binding.request(report, "engineer", target)
    env = {"AGENTIHOOKS_PROFILE": "engineer", binding.HOMES[target]: str(home), binding.REPORT: str(report)}
    monkeypatch.setattr(binding, "process", lambda: (123, target, env, "default"))
    canary = binding.inspect(home, "engineer", target)["canary"]
    binding.validate(canary)
    requested = json.loads(report.read_text())
    requested["validation"][field] = value
    report.write_text(json.dumps(requested))

    with pytest.raises(ValueError, match="^live process binding changed since validation$"):
        binding.validate(canary)
    assert json.loads(report.read_text())["state"] == "failed"


@pytest.mark.parametrize("target", ["claude", "codex"])
@pytest.mark.parametrize("validated", [False, True])
def test_running_session_validates_the_canary_a_later_render_delivered(tmp_path, monkeypatch, target, validated):
    home = tmp_path / "engineer" / target
    home.mkdir(parents=True)
    persona = home / binding.PERSONAS[target]
    persona.write_text(binding.persona("Engineer instructions.\n"))
    (home.parent / f"{target}.sources.json").write_text("[]")
    binding.write(home, "engineer", target)
    report = tmp_path / "report.json"
    binding.request(report, "engineer", target, home)
    env = {"AGENTIHOOKS_PROFILE": "engineer", binding.HOMES[target]: str(home), binding.REPORT: str(report)}
    monkeypatch.setattr(binding, "process", lambda: (123, target, env, "default"))
    launched = binding.inspect(home, "engineer", target)
    if validated:
        binding.validate(launched["canary"])

    persona.write_text(binding.persona("Engineer instructions after the overlay change.\n"))
    binding.write(home, "engineer", target)
    rendered = binding.inspect(home, "engineer", target)
    assert rendered["canary"] != launched["canary"]

    result = binding.validate(rendered["canary"])
    assert result["persona"] == rendered["persona"]
    assert result["pid"] == 123
    assert json.loads(report.read_text())["validation"] == result
    with pytest.raises(ValueError, match="^mounted instruction canary mismatch$"):
        binding.validate("0" * 24)


@pytest.mark.parametrize(
    ("source", "target", "message"),
    [
        ("claude", "codex", "Claude cannot transfer to a Codex account"),
        ("codex", "claude", "Codex cannot transfer to a Claude account"),
        ("codex", "", "Codex cannot transfer to a Claude account"),
        ("", "codex", "Claude cannot transfer to a Codex account"),
        (None, "codex", "Claude cannot transfer to a Codex account"),
    ],
)
def test_quota_transfer_reports_actual_direction_before_launch(tmp_path, monkeypatch, capsys, source, target, message):
    from scripts import init_agent

    monkeypatch.setattr(init_agent, "_write_launcher", lambda *a: pytest.fail("unsupported transfer launched"))
    result = init_agent.main(
        ["--handoff", *(["--agent", target] if target else []), "--dir", str(tmp_path), "--prompt", "continue"],
        {"AGENTIHOOKS_TARGET": source} if source is not None else {},
    )
    assert result == 2
    assert capsys.readouterr().err == f"agentihooks init-agent: unsupported quota transfer: {message}\n"


@pytest.mark.parametrize("target", ["claude", "codex"])
@pytest.mark.parametrize("control", ["valid", "wrong", "missing", "absent", "account"])
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
        lambda *a: ({"AGENTIHOOKS_PROFILE": "engineer", binding.HOMES[target]: str(home)}, a[4]),
    )
    monkeypatch.setattr(init_agent.agent_choice, "choose", lambda *a: (target, "explicit"))
    monkeypatch.setattr(init_agent.claude_trust, "ensure_trusted", lambda *a: ("trusted", ""))
    monkeypatch.setattr(init_agent.herdr_host, "rename_agent", lambda *a: True)

    def launch(launcher, directory, name, args, agent, environ):
        init_agent._started_marker(launcher).touch()
        init_agent._route_report(launcher).write_text("status=routed\naccount=routed-account\n")
        env = dict(environ)
        if control == "wrong":
            env["AGENTIHOOKS_PROFILE"] = "qa"
        if control == "missing":
            env[binding.HOMES[target]] = str(tmp_path / "missing")
        assert env["AGENTIHOOKS_RUN_MODEL"] == "configured-model"
        assert env["AGENTIHOOKS_RUN_EFFORT"] == "medium"
        assert Path(environ[binding.REPORT]).is_file()
        actual_account = "different-account" if control == "account" else "routed-account"
        monkeypatch.setattr(binding, "process", lambda: (123, target, env, actual_account))
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
    native = (
        ["--model", "configured-model", "--effort", "medium"]
        if target == "claude"
        else ["-m", "configured-model", "-c", 'model_reasoning_effort="medium"']
    )
    result = init_agent.main([*args, "--", *native], {"XDG_RUNTIME_DIR": str(tmp_path / "runtime")})
    output = capsys.readouterr()
    from scripts.swarm.runtime import parse_fields

    fields = parse_fields(output.out)
    if control == "valid":
        assert result == 0
        assert fields["profile_validation"] == "validated"
        observed = json.loads(fields["profile_binding"])
        assert (observed["pid"], observed["profile"], observed["harness"], observed["account"]) == (
            123,
            "engineer",
            target,
            "routed-account",
        )
        assert (observed["model"], observed["effort"]) == ("configured-model", "medium")
        assert (fields["model"], fields["effort"]) == ("configured-model", "medium")
    else:
        assert result == 3
        assert fields["profile_validation"] == "failed"
        reasons = {
            "wrong": "live harness profile mismatch with requested choice",
            "missing": f"missing profile home: {tmp_path / 'missing'}",
            "absent": "mounted profile canary did not validate before timeout",
            "account": "live process account differs from requested route",
        }
        assert output.err == f"agentihooks init-agent: {reasons[control]}\n"


@pytest.mark.parametrize("target", ["claude", "codex"])
@pytest.mark.parametrize("pane", ["refused", "routed late"])
def test_a_pane_side_selection_failure_is_reported_before_any_timeout(tmp_path, monkeypatch, capsys, target, pane):
    from scripts import init_agent, select_profile

    home = tmp_path / "engineer" / target
    home.mkdir(parents=True)
    (home / binding.PERSONAS[target]).write_text(binding.persona("Engineer instructions.\n"))
    (home.parent / f"{target}.sources.json").write_text("[]")
    binding.write(home, "engineer", target)
    monkeypatch.setattr(
        select_profile,
        "prepare",
        lambda *a: ({"AGENTIHOOKS_PROFILE": "engineer", binding.HOMES[target]: str(home)}, a[4]),
    )
    monkeypatch.setattr(init_agent.agent_choice, "choose", lambda *a: (target, "explicit"))
    monkeypatch.setattr(init_agent.claude_trust, "ensure_trusted", lambda *a: ("trusted", ""))
    waits = []

    def refuse(*args):
        raise ValueError("profile engineer render failed in the pane")

    def launch(launcher, directory, name, args, agent, environ):
        init_agent._started_marker(launcher).touch()
        report = Path(environ[binding.REPORT])

        def waited(seconds):
            if waits or pane == "refused":
                raise AssertionError(f"waited {seconds}s once the route outcome was known")
            waits.append(seconds)
            init_agent._route_report(launcher).write_text("status=routed\naccount=routed-account\n")
            binding.refuse(report, "mounted canary refused")

        if pane == "refused":
            monkeypatch.setattr(select_profile, "prepare", refuse)
            monkeypatch.setenv(binding.REPORT, str(report))
            assert select_profile.main(["engineer", "--agent", target]) == 2
        monkeypatch.setattr(init_agent.time, "sleep", waited)
        return []

    monkeypatch.setattr(init_agent, "_start_herdr", launch)
    args = ["--host", "herdr", "--agent", target, "--profile", "engineer", "--dir", str(tmp_path)]
    result = init_agent.main([*args, "--route-timeout", "30"], {"XDG_RUNTIME_DIR": str(tmp_path / "runtime")})
    output = capsys.readouterr()
    from scripts.swarm.runtime import parse_fields

    fields = parse_fields(output.out)
    assert result == 3
    assert fields["profile_validation"] == "failed"
    if pane == "refused":
        assert fields["route_status"] == "pending"
        assert output.err == (
            "agentihooks select-profile: profile engineer render failed in the pane\n"
            "agentihooks init-agent: profile selection failed: profile engineer render failed in the pane\n"
        )
    else:
        assert (fields["route_status"], waits) == ("routed", [0.25])
        assert output.err == "agentihooks init-agent: mounted canary refused\n"


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
    assert calls[-1][1:] == ["terminate-agent", "worker", "--force-shared"]


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


def test_only_a_master_quota_transfer_continues_with_an_effort_outside_the_swarm_range(monkeypatch):
    env = {"AGENTIHOOKS_RUN_MODEL": "opus", "AGENTIHOOKS_RUN_EFFORT": "max"}
    monkeypatch.setattr(binding, "process", lambda: (123, "claude", env, "original"))
    swarm = {"AGENTIHOOKS_SWARM_EFFORT_RANGE": "medium:high"}
    master = binding.continuation([], "claude", {**swarm, "AGENTIHOOKS_SWARM_LANE": "master"})
    assert master == ["--model", "opus", "--effort", "max"]
    with pytest.raises(ValueError) as error:
        binding.continuation([], "claude", {**swarm, "AGENTIHOOKS_SWARM_LANE": "eng"})
    assert str(error.value) == "unsupported quota transfer: saved effort is outside the current swarm range"


def test_a_quota_transfer_compares_a_recorded_effort_from_the_other_scale_by_rank(monkeypatch):
    env = {"AGENTIHOOKS_RUN_MODEL": "sol", "AGENTIHOOKS_RUN_EFFORT": "max"}
    monkeypatch.setattr(binding, "process", lambda: (123, "codex", env, "original"))
    swarm = {"AGENTIHOOKS_SWARM_EFFORT_RANGE": "medium:max", "AGENTIHOOKS_SWARM_LANE": "eng"}
    assert binding.continuation([], "codex", swarm) == ["-m", "sol", "-c", 'model_reasoning_effort="xhigh"']


@pytest.mark.parametrize("lane_agent", ["auto", "codex"])
def test_swarm_handoff_keeps_profile_harness_model_effort_and_account(tmp_path, lane_agent):
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
        lanes={"eng": {"agent": lane_agent, "model": "opus", "effort": "high"}},
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
    with pytest.raises(SpawnError, match="^unsupported resume: original profile is missing$"):
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
    with pytest.raises(ValueError, match="^profile canary has no supported live harness ancestor$"):
        binding.process(tmp_path, 1)
    with pytest.raises(ValueError, match="^unsupported live process binding: process filesystem unavailable$"):
        binding.process(tmp_path / "absent", 3)


@pytest.mark.parametrize("args", [["--handoff"], ["--resume", "conversation"]])
def test_continuation_without_required_profile_refuses_before_spawn(tmp_path, monkeypatch, capsys, args):
    from scripts import init_agent

    monkeypatch.setattr(init_agent, "_write_launcher", lambda *a: pytest.fail("missing profile launched"))
    monkeypatch.setattr(init_agent.agent_choice, "choose", lambda *a: ("claude", "explicit"))
    result = init_agent.main([*args, "--dir", str(tmp_path), "--prompt", "continue"], {})
    assert result == 2
    assert (
        capsys.readouterr().err
        == "agentihooks init-agent: unsupported continuation: original required profile is missing; pass --profile\n"
    )


@pytest.mark.parametrize("harness", ["claude", "codex"])
def test_resume_refuses_to_change_recorded_effort_when_policy_changed(tmp_path, harness):
    from dataclasses import replace

    from scripts.swarm.tick import SpawnError
    from tests.swarm.test_runtime import _resuming

    runtime, config, agent, seen = _resuming(tmp_path, "c0ffee", harness)
    config.effort_min = config.effort_max = "high"
    with pytest.raises(
        SpawnError, match="^unsupported transfer: saved effort is outside the current swarm range$"
    ) as error:
        runtime.resume(config, replace(agent, effort="medium"), "continue")
    assert error.value.status == "unsupported"
    assert not seen["runs"]
