import json
import shlex
from pathlib import Path
from unittest.mock import Mock

import pytest

from scripts import init_agent, select_profile
from scripts.profiles import binding


def _rendered(root: Path, target: str, text: str, profile: str = "engineer") -> Path:
    home = root / target
    home.mkdir(parents=True)
    (home / binding.PERSONAS[target]).write_text(binding.persona(text))
    (root / f"{target}.sources.json").write_text("[]")
    binding.write(home, profile, target)
    return home


def _selector_argv(launcher: Path) -> list[str]:
    line = next(line for line in launcher.read_text().splitlines() if " select-profile " in line)
    words = shlex.split(line)
    words = words[words.index("select-profile") + 1 :]
    split = words.index("--")
    return [*words[:split], "--dry-run", *words[split:]]


def _selected(output: str) -> dict[str, str]:
    return dict(line.split("=", 1) for line in output.splitlines() if "=" in line and not line.startswith("argv="))


@pytest.fixture
def world(monkeypatch, tmp_path):
    chain = tmp_path / "chain"
    chain.mkdir()
    from scripts.targets._common import _install_module

    monkeypatch.setattr(_install_module(), "_resolve_profile_chain", lambda name: [(name, chain)])
    monkeypatch.setattr(select_profile.profiles, "_chain", lambda name: [(name, chain)])
    monkeypatch.setattr(select_profile.profiles, "channels", lambda name: "amygdala,brain")
    monkeypatch.setattr(init_agent.agent_choice, "choose", lambda *a: (a[0], "explicit"))
    monkeypatch.setattr(init_agent.claude_trust, "ensure_trusted", lambda *a: ("trusted", ""))
    monkeypatch.setattr(init_agent.herdr_host, "rename_agent", lambda *a: True)
    return tmp_path


def _launch(monkeypatch, world, capsys, target, between, continued=()):
    prepared = world / "prepared"
    recorded = _rendered(prepared, target, "Instructions init agent prepared.\n")
    canary = binding.inspect(recorded, "engineer", target)["canary"]
    renderer = Mock()
    monkeypatch.setattr(select_profile.profiles, "render", renderer)
    monkeypatch.setattr(select_profile.profiles, "profile_dir", lambda name, worn=(): prepared)
    seen = {}

    def start(launcher, directory, name, args, agent, environ):
        assert (directory, name, agent) == (world, "recorded", target)
        init_agent._started_marker(launcher).touch()
        init_agent._route_report(launcher).write_text("status=routed\naccount=routed-account\n")
        between(renderer)
        capsys.readouterr()
        seen["selector"] = select_profile.main(_selector_argv(launcher))
        selected = {**_selected(capsys.readouterr().out), binding.REPORT: environ[binding.REPORT]}
        seen["home"] = selected.get(binding.HOMES[target])
        monkeypatch.setattr(binding, "process", lambda: (123, target, selected, "routed-account"))
        binding.validate(canary)
        return []

    monkeypatch.setattr(init_agent, "_start_herdr", start)
    argv = ["--host", "herdr", "--agent", target, "--profile", "engineer", "--dir", str(world), "--name", "recorded"]
    argv += [*continued, "--route-timeout", "0.1"]
    result = init_agent.main(argv, {"XDG_RUNTIME_DIR": str(world / "runtime")})
    from scripts.swarm.runtime import parse_fields

    return result, parse_fields(capsys.readouterr().out), seen, recorded, renderer


@pytest.mark.parametrize("continued", [(), ("--resume", "conversation")])
@pytest.mark.parametrize("target", ["claude", "codex"])
def test_a_package_change_between_prepare_and_launch_mounts_the_recorded_home(
    monkeypatch, world, capsys, target, continued
):
    pulled = world / "pulled"
    _rendered(pulled, target, "Instructions a later pull of dev renders.\n")

    def pull(renderer):
        renderer.reset_mock()
        monkeypatch.setattr(select_profile.profiles, "profile_dir", lambda name, worn=(): pulled)

    result, fields, seen, recorded, renderer = _launch(monkeypatch, world, capsys, target, pull, continued)
    assert seen == {"selector": 0, "home": str(recorded)}
    renderer.assert_not_called()
    assert result == 0
    assert fields["profile_validation"] == "validated"
    assert json.loads(fields["profile_binding"])["home"] == str(recorded.resolve())


@pytest.mark.parametrize("target", ["claude", "codex"])
def test_a_launcher_on_another_code_root_mounts_the_recorded_home(monkeypatch, world, capsys, target):
    def other_root(renderer):
        renderer.side_effect = ValueError(
            "this run comes from another checkout, so it renders only into a scratch home"
        )
        monkeypatch.setattr(select_profile.profiles, "profile_dir", lambda name, worn=(): None)

    result, fields, seen, recorded, renderer = _launch(monkeypatch, world, capsys, target, other_root)
    assert seen == {"selector": 0, "home": str(recorded)}
    assert result == 0
    assert fields["profile_validation"] == "validated"
    assert json.loads(fields["profile_binding"])["home"] == str(recorded.resolve())


@pytest.mark.parametrize("target", ["claude", "codex"])
def test_a_recorded_home_of_another_profile_is_refused_and_fails_the_launch_report(monkeypatch, world, capsys, target):
    home = _rendered(world / "qa", target, "QA instructions.\n", profile="qa")
    report = world / "report.json"
    binding.request(report, "engineer", target)
    monkeypatch.setenv(binding.REPORT, str(report))
    renderer = Mock()
    monkeypatch.setattr(select_profile.profiles, "render", renderer)
    argv = ["engineer", f"--home={home}", "--agent", target, "--dry-run", "--"]
    assert select_profile.main(argv) == 2
    renderer.assert_not_called()
    assert capsys.readouterr().err == "agentihooks select-profile: profile mismatch in rendered binding\n"
    assert json.loads(report.read_text()) == {
        "profile": "engineer",
        "harness": target,
        "state": "failed",
        "reason": "profile selection failed: profile mismatch in rendered binding",
    }


@pytest.mark.parametrize("operator", [False, True])
def test_a_swarm_codex_selector_mounts_its_recorded_home_but_never_the_operator_home(
    monkeypatch, world, capsys, operator
):
    home = _rendered(world / "isolated", "codex", "Isolated swarm Codex home.\n")
    monkeypatch.setattr(
        select_profile.profiles, "operator_codex_home", lambda: home if operator else world / "operator"
    )
    renderer = Mock()
    monkeypatch.setattr(select_profile.profiles, "render", renderer)
    monkeypatch.setenv("AGENTIHOOKS_SWARM", "slug")
    argv = ["engineer", f"--home={home}", "--agent", "codex", "--dry-run", "--"]
    assert select_profile.main(argv) == (2 if operator else 0)
    renderer.assert_not_called()
    output = capsys.readouterr()
    if operator:
        assert output.err == "agentihooks select-profile: swarm codex profile resolves to the operator Codex home\n"
    else:
        assert _selected(output.out)["CODEX_HOME"] == str(home)


@pytest.mark.parametrize("target", ["claude", "codex"])
def test_init_agent_renders_even_when_its_caller_carries_a_recorded_home(monkeypatch, world, target):
    stale = _rendered(world / "stale", target, "A home a caller left behind.\n")
    renderer = Mock()
    monkeypatch.setattr(select_profile.profiles, "render", renderer)
    monkeypatch.setattr(select_profile.profiles, "profile_dir", lambda name, worn=(): world / "prepared")
    monkeypatch.setattr(init_agent, "_launch_command", lambda *args: ("linux", ["terminal"]))
    monkeypatch.setattr(init_agent.shutil, "which", lambda name: "/bin/agentihooks" if name == "agentihooks" else None)
    environ = {"XDG_RUNTIME_DIR": str(world / "runtime"), select_profile.RECORDED_HOME: str(stale)}
    argv = ["--agent", target, "--profile", "engineer", "--dir", str(world), "--dry-run"]
    assert init_agent.main(argv, environ) == 0
    renderer.assert_called_once_with(target, "engineer", overlays=[], bundle_revision="")
    launcher = next((world / "runtime").rglob("*.sh"))
    assert _selector_argv(launcher)[:2] == ["engineer", f"--home={world / 'prepared' / target}"]


@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_the_selector_command_carries_the_recorded_home(monkeypatch, tmp_path, agent):
    monkeypatch.setattr(init_agent.shutil, "which", lambda name: "/bin/agentihooks")
    spec = init_agent.AgentSpec(agent=agent, profile="engineer", home="/homes/engineer/claude")
    command, _ = init_agent._agent_command(spec, tmp_path / "route", "run", [], {}, tmp_path)
    assert command[:6] == [
        "/bin/agentihooks",
        "select-profile",
        "engineer",
        "--home=/homes/engineer/claude",
        "--agent",
        agent,
    ]
