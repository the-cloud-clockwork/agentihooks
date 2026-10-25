import argparse
import json
import os
import shlex
import subprocess
import sys

import pytest

from scripts import init_agent

pytestmark = pytest.mark.unit


def app_server_spec(**changes):
    return init_agent.AgentSpec(
        agent="codex",
        profile="engineer",
        transport=init_agent.APP_SERVER,
        seat="eng-1@sw",
        **changes,
    )


def launched(tmp_path, spec, prompt="Open the task."):
    launcher, prompt_file = init_agent._write_launcher(
        tmp_path, "engineer@abc-0001", prompt, ["-m", "gpt-6.1-sol"], {"XDG_RUNTIME_DIR": str(tmp_path)}, spec
    )
    line = next(line for line in launcher.read_text().splitlines() if line.startswith("setsid -f "))
    server, _, _ = shlex.split(line)[4].removeprefix(init_agent.LAUNCHER_PIN).partition("; rm -f ")
    return launcher, prompt_file, shlex.split(server)


def test_the_app_server_branch_runs_the_supervisor_with_the_opening_prompt_file(tmp_path):
    spec = app_server_spec(
        overlays=("frontend",), bundle_revision="abc", previous_execution="exe-prev", resume="thread-kept"
    )
    launcher, prompt_file, argv = launched(tmp_path, spec)
    split = argv.index("--")
    assert argv[:4] == [sys.executable, "-m", "scripts.codex_session", "serve"]
    assert argv[argv.index("--cwd") + 1] == str(tmp_path)
    assert argv[argv.index("--profile") + 1] == "engineer"
    assert argv[argv.index("--seat") + 1] == "eng-1@sw"
    assert "--overlay=frontend" in argv and "--bundle-revision=abc" in argv
    assert argv[argv.index("--previous-execution") + 1] == "exe-prev"
    assert argv[argv.index("--resume") + 1] == "thread-kept"
    assert argv[argv.index("--report") + 1] == str(init_agent._route_report(launcher))
    assert prompt_file.suffix == ".opening"
    assert argv[argv.index("--prompt-file") + 1] == str(prompt_file)
    assert argv[-2:] == ["-m", "gpt-6.1-sol"]
    assert split < len(argv) - 2
    assert "--no-daemon" not in argv


def test_the_detached_server_shell_is_the_swarm_launcher_of_the_app_server(tmp_path):
    probe = "sh -c 'echo \"$AGENTIHOOKS_SWARM_LAUNCHER $PPID\"'"
    inputs = [str(tmp_path / "input")]
    line = init_agent._viewer_lines(probe, tmp_path / "x.sh", "engineer@abc-0001", {"AGENTIHOOKS_SWARM": "sw"}, inputs)
    script = shlex.split(line.splitlines()[0])[4]
    environ = {**os.environ, "AGENTIHOOKS_SWARM_LAUNCHER": "1"}
    launcher, parent = subprocess.run(
        ["sh", "-c", script], env=environ, capture_output=True, text=True, check=True
    ).stdout.split()
    assert launcher == parent


def test_the_app_server_branch_never_hands_the_prompt_to_the_pane(tmp_path):
    launcher, prompt_file, argv = launched(tmp_path, app_server_spec())
    text = launcher.read_text()
    assert init_agent.PROMPT_AFTER_START not in text
    assert "$(cat" not in text
    assert init_agent._held_prompt(launcher, "codex") == ""
    assert prompt_file.read_text(encoding="utf-8") == "Open the task."


def test_the_app_server_branch_passes_the_interactive_route(tmp_path):
    _, _, argv = launched(tmp_path, app_server_spec(auth="interactive"))
    assert argv[argv.index("--") + 1 : argv.index("--") + 3] == ["--route", "interactive"]


def test_a_legacy_codex_launch_keeps_the_interactive_terminal(tmp_path):
    launcher, prompt_file = init_agent._write_launcher(
        tmp_path, "m", "Open.", [], {"XDG_RUNTIME_DIR": str(tmp_path)}, init_agent.AgentSpec(agent="codex")
    )
    assert "agentihooks codex " in launcher.read_text()
    assert "scripts.codex_session" not in launcher.read_text()
    assert prompt_file.suffix == ".prompt"


def namespace(**changes):
    values = {"codex_transport": init_agent.APP_SERVER, "seat": "eng-1@sw", "profile": "engineer"}
    return argparse.Namespace(**{**values, **changes})


def test_the_transport_flag_needs_codex_a_seat_and_a_profile():
    assert init_agent._transport(namespace(), "codex") == init_agent.APP_SERVER
    assert init_agent._transport(namespace(codex_transport=""), "claude") == ""
    assert init_agent._transport(namespace(codex_transport="legacy"), "codex") == "legacy"
    with pytest.raises(ValueError, match="runs only a Codex agent"):
        init_agent._transport(namespace(), "claude")
    for missing in ("seat", "profile"):
        with pytest.raises(ValueError, match="needs --seat and --profile"):
            init_agent._transport(namespace(**{missing: ""}), "codex")


def test_the_parser_takes_the_transport_seat_and_predecessor():
    args = init_agent._parser().parse_args(
        ["--codex-transport", "app_server", "--seat", "eng-1@sw", "--previous-execution", "exe-prev"]
    )
    assert (args.codex_transport, args.seat, args.previous_execution) == ("app_server", "eng-1@sw", "exe-prev")
    with pytest.raises(SystemExit):
        init_agent._parser().parse_args(["--codex-transport", "pane"])


def test_the_transport_flags_default_to_empty_and_take_legacy():
    args = init_agent._parser().parse_args([])
    assert (args.codex_transport, args.seat, args.previous_execution, args.execution) == ("", "", "", "")
    assert init_agent._parser().parse_args(["--codex-transport", "legacy"]).codex_transport == "legacy"


def test_the_execution_flag_names_the_admitted_execution_in_its_help():
    [action] = [action for action in init_agent._parser()._actions if action.dest == "execution"]
    assert action.help == "Codex app-server: the admitted execution this launch serves"


def test_the_transport_refusals_name_the_flag_exactly():
    with pytest.raises(ValueError) as refused:
        init_agent._transport(namespace(), "claude")
    assert str(refused.value) == "--codex-transport app_server runs only a Codex agent"
    with pytest.raises(ValueError) as refused:
        init_agent._transport(namespace(seat=""), "codex")
    assert str(refused.value) == "--codex-transport app_server needs --seat and --profile"


def test_the_app_server_command_passes_the_exclusion_trust_and_model_flags(tmp_path):
    argv = init_agent._app_server_command(
        app_server_spec(exclude="old"), tmp_path / "route", ["-c", "x=1"], {}, tmp_path, None
    )
    assert argv[argv.index("--exclude") : argv.index("--exclude") + 2] == ["--exclude", "old"]
    assert argv[argv.index("--") + 1 :] == [
        "-c",
        f'projects={{{json.dumps(str(tmp_path))}={{trust_level="trusted"}}}}',
        "-m",
        "gpt-6.1-sol",
        "-c",
        'model_reasoning_effort="medium"',
        "-c",
        "x=1",
    ]


def test_main_hands_the_transport_seat_and_predecessor_to_the_launcher(tmp_path, monkeypatch, capsys):
    built = []

    def write_launcher(directory, name, prompt, claude_args, environ, spec):
        built.append(spec)
        raise ValueError("captured")

    monkeypatch.setattr(init_agent, "_prepare_profile", lambda args, agent, flags, environ: flags)
    monkeypatch.setattr(init_agent, "_write_launcher", write_launcher)
    argv = [
        "--dir",
        str(tmp_path),
        "--agent",
        "codex",
        "--profile",
        "engineer",
        "--codex-transport",
        "app_server",
        "--seat",
        "eng-1@sw",
        "--previous-execution",
        "exe-prev",
        "--execution",
        "exe-admitted",
        "--dry-run",
    ]
    assert init_agent.main(argv, {"HOME": str(tmp_path), "XDG_RUNTIME_DIR": str(tmp_path / "rt")}) == 2
    assert capsys.readouterr().err == "agentihooks init-agent: captured\n"
    [spec] = built
    assert (spec.transport, spec.seat, spec.previous_execution) == (init_agent.APP_SERVER, "eng-1@sw", "exe-prev")
    assert spec.execution == "exe-admitted"


def test_the_app_server_command_names_the_admitted_execution_only_when_given(tmp_path):
    admitted = init_agent._app_server_command(
        app_server_spec(execution="exe-admitted"), tmp_path / "route", [], {}, tmp_path, None
    )
    assert admitted[admitted.index("--execution") + 1] == "exe-admitted"
    assert admitted.index("--execution") < admitted.index("--")
    plain = init_agent._app_server_command(app_server_spec(), tmp_path / "route", [], {}, tmp_path, None)
    assert "--execution" not in plain


def imported_scripts(interpreter, directory):
    shadow = directory / "scripts"
    shadow.mkdir(parents=True)
    (shadow / "__init__.py").write_text("")
    probe = [*interpreter[: interpreter.index("-m")], "-c", "import scripts; print(scripts.__file__)"]
    return subprocess.run(probe, cwd=directory, capture_output=True, text=True, check=True).stdout.strip()


def test_the_app_server_imports_the_launching_package_never_the_agent_directory(tmp_path):
    import scripts

    _, _, argv = launched(tmp_path, app_server_spec())
    assert imported_scripts(argv, tmp_path / "agent") == scripts.__file__


def test_the_viewer_imports_the_launching_package_never_the_agent_directory(tmp_path):
    import scripts

    line = init_agent._viewer_lines("serve", tmp_path / "x.sh", "engineer@abc-0001", {"AGENTIHOOKS_SWARM": "sw"}, [])
    assert imported_scripts(shlex.split(line.splitlines()[1]), tmp_path / "agent") == scripts.__file__


@pytest.mark.parametrize(("transport", "served"), [("app_server", True), ("legacy", False)])
def test_main_names_the_pane_as_served_only_on_the_app_server_transport(
    tmp_path, monkeypatch, capsys, transport, served
):
    named = []

    def start(launcher, directory, name, args, agent, environ):
        init_agent._started_marker(launcher).touch()
        init_agent._route_report(launcher).write_text("status=routed\naccount=a\n")
        return ["pane_id=w1:p2"]

    monkeypatch.setattr(init_agent, "_prepare_profile", lambda args, agent, flags, environ: flags)
    monkeypatch.setattr(init_agent, "_binding_request", lambda args, agent, prompt, environ, flags: prompt)
    monkeypatch.setattr(init_agent, "_start_herdr", start)
    monkeypatch.setattr(init_agent, "_name_in_herdr", lambda *args, **kwargs: named.append(kwargs) or [])
    monkeypatch.setattr(init_agent, "_route_reported", lambda *args: None)
    monkeypatch.setattr(init_agent, "_binding_result", lambda *args: [])
    monkeypatch.setattr(init_agent.herdr_panes, "mark", lambda *args, **changes: None)
    argv = [
        "--host",
        "herdr",
        "--dir",
        str(tmp_path),
        "--name",
        "eng-a",
        "--agent",
        "codex",
        "--profile",
        "engineer",
        "--codex-transport",
        transport,
        "--seat",
        "eng-1@sw",
        "--route-timeout",
        "0.1",
    ]
    assert init_agent.main(argv, {"HOME": str(tmp_path), "XDG_RUNTIME_DIR": str(tmp_path / "rt")}) == 0
    assert named == [{"served": served}]
