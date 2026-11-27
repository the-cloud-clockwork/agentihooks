import json
import os
import shlex
import stat
import sys
from collections.abc import Callable, Mapping
from pathlib import Path

import pytest

from hooks import proc
from scripts.quick import launch, member, paths, recipe, registry, seeds, tick
from scripts.quick.guard import QuickError

pytestmark = pytest.mark.unit

TOOL = "/usr/bin/env"


def _swarm(folder: Path, command: tuple[str, ...], harness: str = "program") -> registry.Swarm:
    seats = (
        registry.Seat(1, "master", "w1:p1", "claude", "quick-abc-master", session_id="s-master"),
        registry.Seat(2, "eng-1", "w1:p2", harness, "quick-abc-eng-1", command=command),
    )
    swarm = registry.Swarm("quick-abc", "/work dir", seats)
    paths.create(folder, [seat.seat for seat in seats])
    registry.save(folder, swarm)
    return swarm


def _seat(folder: Path) -> registry.Seat:
    swarm = registry.load(folder)
    assert swarm is not None
    return swarm.seats[1]


class Panes:
    def __init__(self, write_pid: bool = True) -> None:
        self.runs: list[tuple[str, Path]] = []
        self.write_pid = write_pid
        self.environ: dict[str, str] = {}

    def __call__(self, environ: dict[str, str]) -> "Panes":
        self.environ = dict(environ)
        return self

    def run(self, pane_id: str, launcher: Path) -> bool:
        self.runs.append((pane_id, launcher))
        if self.write_pid:
            recipe.record(launcher.parent / recipe.PID, os.getpid())
        return True


class Herdr:
    def __init__(self, bare: bool | None = False) -> None:
        self.prompts: list[tuple[str, str]] = []
        self.shell = bare

    def status(self, pane_id: str) -> str:
        return "idle"

    def typed(self, pane_id: str) -> str:
        return ""

    def bare(self, pane_id: str) -> bool | None:
        return self.shell

    def screen(self, pane_id: str) -> str:
        return "Not logged in · Run /login"

    def prompt(self, pane_id: str, text: str) -> bool:
        self.prompts.append((pane_id, text))
        return True


@pytest.mark.feature("quick-local-command")
def test_a_json_argument_list_parses_to_its_literal_arguments() -> None:
    assert recipe.parse('["my wrapper", "--flag=$HOME", "a;b|c", "*"]') == ("my wrapper", "--flag=$HOME", "a;b|c", "*")


@pytest.mark.feature("quick-local-command")
@pytest.mark.parametrize("text", ["", "my-tool --flag", "[]", '"tool"', '[""]', "[1]", '["a\\nb"]', '["a\\u0000b"]'])
def test_anything_but_a_list_of_plain_arguments_is_refused(text: str) -> None:
    with pytest.raises(QuickError):
        recipe.parse(text)


@pytest.mark.feature("quick-local-command")
def test_a_common_command_applies_to_every_engineer_and_an_override_replaces_one() -> None:
    found = recipe.recipes(f'["{TOOL}", "a"]', [f'2=["{TOOL}", "b"]'], ["claude", "codex", "program"], {})
    assert found == [(TOOL, "a"), (TOOL, "b"), (TOOL, "a")]


@pytest.mark.feature("quick-local-command")
def test_no_command_keeps_every_engineer_on_its_built_in_launch() -> None:
    assert recipe.recipes("", [], ["claude", "codex"], {}) == [(), ()]


@pytest.mark.feature("quick-local-command")
@pytest.mark.parametrize("override", ['3=["/usr/bin/env"]', '0=["/usr/bin/env"]', 'x=["/usr/bin/env"]', "1"])
def test_an_override_for_an_engineer_that_does_not_exist_is_refused(override: str) -> None:
    with pytest.raises(QuickError, match="--engineer-command"):
        recipe.recipes("", [override], ["claude", "codex"], {})


@pytest.mark.feature("quick-local-command")
def test_a_program_engineer_without_a_command_is_refused() -> None:
    with pytest.raises(QuickError, match="program engineer needs a command"):
        recipe.recipes("", [], ["claude", "program"], {})


@pytest.mark.feature("quick-local-command")
def test_a_missing_executable_is_refused_naming_only_its_file_name(tmp_path: Path) -> None:
    with pytest.raises(QuickError) as refused:
        recipe.recipes(f'["{tmp_path}/nowhere/tool", "--opaque-value"]', [], ["program"], {})
    assert (
        "tool" in str(refused.value) and "opaque" not in str(refused.value) and str(tmp_path) not in str(refused.value)
    )


@pytest.mark.feature("quick-local-command")
def test_an_executable_is_found_on_the_callers_path_or_refused(tmp_path: Path) -> None:
    tool = tmp_path / "my tool"
    tool.write_text("#!/bin/sh\n")
    tool.chmod(tool.stat().st_mode | stat.S_IXUSR)
    assert recipe.recipes('["my tool"]', [], ["program"], {"PATH": str(tmp_path)}) == [(str(tool),)]
    with pytest.raises(QuickError):
        recipe.recipes('["my tool"]', [], ["program"], {"PATH": str(tmp_path / "empty")})


@pytest.mark.feature("quick-local-command")
def test_an_argument_that_looks_like_a_credential_is_refused() -> None:
    token = "ghp_" + "a1" * 18
    with pytest.raises(QuickError, match="credential") as refused:
        recipe.recipes(f'["{TOOL}", "--token", "{token}"]', [], ["program"], {})
    assert token not in str(refused.value)
    assert str(refused.value) == (
        "a command argument looks like a credential; let the program read it from its own environment"
    )


@pytest.mark.feature("quick-local-command")
def test_a_seat_kind_follows_its_harness_and_command() -> None:
    native = registry.Seat(2, "eng-1", "w1:p2", "claude", "n")
    wrapper = registry.Seat(2, "eng-1", "w1:p2", "codex", "n", command=("my-codex",))
    program = registry.Seat(2, "eng-1", "w1:p2", "program", "n", command=("/opt/tools/watcher", "--once"))
    assert [recipe.kind(seat) for seat in (native, wrapper, program)] == ["native", "wrapper", "program"]
    assert (
        recipe.capabilities(native)
        == recipe.capabilities(wrapper)
        == {
            "agent_hooks": True,
            "automatic_handoff": True,
            "prompt_typing": True,
        }
    )
    assert recipe.capabilities(program) == {"agent_hooks": False, "automatic_handoff": False, "prompt_typing": False}
    assert recipe.identity(program) == {"kind": "program", "program": "watcher", "arguments": 1}
    assert recipe.identity(native) == {"kind": "native", "program": "claude", "arguments": 0}


@pytest.mark.feature("quick-local-command")
def test_the_launcher_keeps_every_argument_literal_and_records_its_process(tmp_path: Path) -> None:
    argv = ("my wrapper", "$HOME", "a;b", "'q'")
    text = recipe.script(argv, {"AGENTIHOOKS_QUICK_SEAT": "eng 1"}, tmp_path / "pid file", "/work dir")
    lines = text.splitlines()
    assert lines[0] == "#!/usr/bin/env bash"
    assert lines[1] == "set -euo pipefail"
    assert "export AGENTIHOOKS_QUICK_SEAT='eng 1'" in lines
    assert f"if cd -- {shlex.quote('/work dir')}; then" in lines
    assert any("run_program" in line and shlex.quote(str(tmp_path / "pid file")) in line for line in lines)
    assert lines[-1] == "exec /bin/bash -l"
    assert shlex.split(lines[-3].removesuffix(" || true"))[-len(argv) :] == list(argv)


@pytest.mark.feature("quick-local-command")
def test_a_command_seat_runs_its_launcher_in_its_own_pane_without_init_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    swarm = _swarm(tmp_path, (TOOL, "two words"), harness="program")
    panes = Panes()
    monkeypatch.setattr(tick, "LiveHerdr", panes)

    def refuse(arguments: list[str], env: dict[str, str]) -> int:
        pytest.fail("a command seat called init-agent")

    assert launch.start_seat(tmp_path, swarm, swarm.seats[1], {"PATH": "/bin"}, refuse) == 0
    [(pane, launcher)] = panes.runs
    text = launcher.read_text()
    assert pane == "w1:p2" and stat.S_IMODE(launcher.stat().st_mode) == 0o700
    assert panes.environ == launch.environ({"PATH": "/bin"}, tmp_path, swarm, swarm.seats[1])
    assert "export PATH=/bin" in text
    assert f"if cd -- {shlex.quote(swarm.directory)}; then" in text
    assert shlex.quote(str(paths.seat_dir(tmp_path, "eng-1") / recipe.PID)) in text
    assert recipe.PROMPT_ENV not in text
    assert "export AGENTIHOOKS_QUICK_SEAT=eng-1" in text and f"export AGENTIHOOKS_QUICK_DIR={tmp_path}" in text
    assert shlex.split(text.splitlines()[-3].removesuffix(" || true"))[-2:] == [TOOL, "two words"]


@pytest.mark.feature("quick-local-command")
def test_a_plain_program_never_receives_the_prompt_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    swarm = _swarm(tmp_path, (TOOL,))
    panes = Panes()
    monkeypatch.setattr(tick, "LiveHerdr", panes)
    assert launch.start_seat(tmp_path, swarm, swarm.seats[1], {}, launch.init_agent) == 0
    assert recipe.PROMPT_ENV not in panes.runs[0][1].read_text()


@pytest.mark.feature("quick-local-command")
def test_a_pane_herdr_refuses_fails_the_command_seat_start(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    swarm = _swarm(tmp_path, (TOOL,))

    class Refusing(Panes):
        def run(self, pane_id: str, launcher: Path) -> bool:
            return False

    monkeypatch.setattr(tick, "LiveHerdr", Refusing())
    assert launch.start_seat(tmp_path, swarm, swarm.seats[1], {}, launch.init_agent) == 1


@pytest.mark.feature("quick-local-command")
def test_start_all_starts_command_and_native_engineers_each_their_own_way(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    swarm = _swarm(tmp_path, (TOOL,))
    native = registry.Seat(3, "eng-2", "w1:p3", "codex", "quick-abc-eng-2")
    swarm = registry.Swarm(swarm.quick_id, swarm.directory, (*swarm.seats, native))
    registry.save(tmp_path, swarm)
    paths.create(tmp_path, ["eng-2"])
    panes = Panes()
    monkeypatch.setattr(tick, "LiveHerdr", panes)
    started: list[list[str]] = []

    class Process:
        def wait(self) -> int:
            return 0

    def popen(command: list[str], env: dict[str, str]) -> Process:
        started.append(command)
        return Process()

    monkeypatch.setattr(launch.subprocess, "Popen", popen)
    assert launch.start_all(tmp_path, swarm, {}) == {"eng-1": 0, "eng-2": 0}
    assert [pane for pane, _ in panes.runs] == ["w1:p2"]
    assert [command[:3] for command in started] == [[sys.executable, "-m", "scripts.init_agent"]]
    assert "--pane" in started[0] and started[0][started[0].index("--pane") + 1] == "w1:p3"


@pytest.mark.feature("quick-local-command")
def test_a_relaunched_plain_program_is_ready_on_its_live_process_without_a_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _swarm(tmp_path, (TOOL,))
    registry.set_state(tmp_path, "eng-1", registry.PENDING)
    panes = Panes()
    monkeypatch.setattr(tick, "LiveHerdr", panes)
    shells = iter([True])
    outcome = launch.relaunch(
        tmp_path, "eng-1", {}, bare=lambda pane: next(shells, False), sleep=lambda seconds: None, wait_s=0.0
    )
    seat = _seat(tmp_path)
    assert outcome == "started" and seat.state == registry.READY and seat.session_id == ""
    assert recipe.alive(paths.seat_dir(tmp_path, "eng-1"))


@pytest.mark.feature("quick-local-command")
def test_a_plain_program_whose_process_is_gone_leaves_the_seat_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _swarm(tmp_path, (TOOL,))
    registry.set_state(tmp_path, "eng-1", registry.PENDING)
    monkeypatch.setattr(tick, "LiveHerdr", Panes(write_pid=False))
    shells = iter([True])
    outcome = launch.relaunch(
        tmp_path, "eng-1", {}, bare=lambda pane: next(shells, False), sleep=lambda seconds: None, wait_s=0.0
    )
    assert outcome == "unbound" and _seat(tmp_path).state == registry.PENDING


@pytest.mark.feature("quick-local-command")
def test_a_relaunched_wrapper_still_waits_for_its_native_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _swarm(tmp_path, (TOOL,), harness="claude")
    registry.set_state(tmp_path, "eng-1", registry.PENDING)
    calls: list[list[str]] = []

    def start(arguments: list[str], environ: dict[str, str]) -> int:
        calls.append(arguments)
        return 0

    shells = iter([True])
    outcome = launch.relaunch(
        tmp_path,
        "eng-1",
        {},
        bare=lambda pane: next(shells, False),
        start=start,
        sleep=lambda seconds: None,
        wait_s=0.0,
    )
    assert outcome == "unbound" and len(calls) == 1
    assert calls[0][calls[0].index("--agent") + 1] == "claude"


@pytest.mark.feature("quick-local-command")
def test_force_and_relaunch_reuse_the_saved_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _swarm(tmp_path, (TOOL, "keep me"))
    monkeypatch.setattr(recipe, "stop", lambda folder: True)
    panes = Panes()
    monkeypatch.setattr(tick, "LiveHerdr", panes)
    outcome = launch.force(
        tmp_path, "eng-1", {}, bare=lambda pane: True, end=lambda name: None, sleep=lambda seconds: None
    )
    assert outcome == "exited"
    registry.set_state(tmp_path, "eng-1", registry.PENDING)
    launch.relaunch(tmp_path, "eng-1", {}, bare=lambda pane: True, sleep=lambda seconds: None, wait_s=0.0)
    assert [
        shlex.split(launcher.read_text().splitlines()[-3].removesuffix(" || true"))[-2:] for _, launcher in panes.runs
    ] == [[TOOL, "keep me"]] * 2


@pytest.mark.feature("quick-local-command")
def test_the_tick_never_types_into_a_plain_program_seat(tmp_path: Path) -> None:
    _swarm(tmp_path, (TOOL,))
    herdr = Herdr()
    rows = tick.round_for(tmp_path, herdr, lambda folder, seat: "relaunched", 10_000.0, lambda folder, seat: "forced")
    assert rows[1] == {"seat": "eng-1", "outcome": "skipped-program"}
    assert herdr.prompts == []
    assert tick.tell(tmp_path, _seat(tmp_path), herdr) == "skipped-program"
    assert herdr.prompts == []


@pytest.mark.feature("quick-swarm")
def test_a_registry_saved_before_newer_seat_fields_loads_with_their_defaults(tmp_path: Path) -> None:
    seat = {"position": 2, "seat": "eng-1", "pane_id": "w1:p2", "harness": "codex", "name": "quick-abc-eng-1"}
    registry.path(tmp_path).write_text(
        f'{{"quick_id": "quick-abc", "directory": "/work", "seats": [{json.dumps(seat)}]}}'
    )
    swarm = registry.load(tmp_path)
    assert swarm is not None
    assert swarm.seats == (registry.Seat(2, "eng-1", "w1:p2", "codex", "quick-abc-eng-1"),)
    assert swarm.seats[0].command == () and swarm.seats[0].auth == "routed"


@pytest.mark.feature("quick-local-command")
def test_a_saved_command_round_trips_through_the_registry_as_literal_arguments(tmp_path: Path) -> None:
    saved = _swarm(tmp_path, ("my tool", "a;b", "$HOME"))
    assert registry.load(tmp_path) == saved
    assert _seat(tmp_path).command == ("my tool", "a;b", "$HOME")


@pytest.mark.feature("quick-local-command")
@pytest.mark.parametrize("harness", ["claude", "codex"])
@pytest.mark.parametrize("auth", ["routed", "default", "named"])
def test_native_wrappers_use_the_saved_authentication_launcher(tmp_path: Path, harness: str, auth: str) -> None:
    import dataclasses

    swarm = _swarm(tmp_path, (TOOL, "literal;$HOME"), harness)
    seat = dataclasses.replace(swarm.seats[1], auth=auth, account="alpha" if auth == "named" else "")
    calls: list[tuple[list[str], dict[str, str]]] = []

    def start(arguments: list[str], environ: dict[str, str]) -> int:
        calls.append((arguments, environ))
        return 0

    assert launch.start_seat(tmp_path, swarm, seat, {"AGENTIHOOKS_ROUTE_ACCOUNT": "other"}, start) == 0
    [(arguments, environ)] = calls
    assert arguments == launch.argv(swarm, seat, paths.opening(tmp_path, seat.seat))
    if auth == "routed":
        assert "--routed-only" in arguments
    else:
        assert arguments[-2:] == ["--auth", "named:alpha" if auth == "named" else "default"]
        assert "--routed-only" not in arguments
    assert environ[member.SEAT_ENV] == seat.seat


@pytest.mark.feature("quick-local-command")
@pytest.mark.parametrize("harness", ["claude", "codex"])
def test_native_exec_preserves_routed_environment_and_literal_wrapper_prefix(tmp_path: Path, harness: str) -> None:
    command = (TOOL, "two words", "$HOME;literal")
    swarm = _swarm(tmp_path, command, harness)
    environ = launch.environ(
        {
            "AGENTIHOOKS_ROUTE_ACCOUNT": "alpha",
            "AGENTIHOOKS_PROFILE_REPORT": "/profile/report",
            "CLAUDE_CONFIG_DIR": "/native/claude",
            "CODEX_HOME": "/native/codex",
        },
        tmp_path,
        swarm,
        swarm.seats[1],
    )
    received: list[tuple[str, list[str], dict[str, str]]] = []

    def execute(binary: str, argv: list[str], child: Mapping[str, str]) -> None:
        received.append((binary, argv, dict(child)))

    native = ["native", "--model", "chosen", "opening prompt"]
    recipe.execute_native(harness, native, environ, execvpe=execute)
    [(binary, argv, child)] = received
    assert binary == TOOL and argv == [*command, *native[1:]]
    assert child[recipe.PROMPT_ENV] == str(paths.opening(tmp_path, "eng-1"))
    for name, value in environ.items():
        assert child[name] == value
    assert swarm.seats[1].command == command


@pytest.mark.feature("quick-local-command")
def test_the_program_launcher_stops_when_its_directory_is_missing(tmp_path: Path) -> None:
    import subprocess

    result = tmp_path / "must not run"
    launcher = tmp_path / "launcher.sh"
    command = (sys.executable, "-c", "from pathlib import Path; import sys; Path(sys.argv[1]).touch()", str(result))
    launcher.write_text(recipe.script(command, {}, tmp_path / recipe.PID, str(tmp_path / "missing")))
    completed = subprocess.run(
        ["bash", str(launcher)],
        input="printf 'shell-restored\\n'\nexit\n",
        text=True,
        capture_output=True,
        check=False,
        timeout=15,
    )
    assert completed.returncode == 0
    assert "shell-restored" in completed.stdout
    assert "missing" in completed.stderr
    assert not result.exists() and not (tmp_path / recipe.PID).exists()


@pytest.mark.feature("quick-local-command")
@pytest.mark.parametrize("state", ["Z", "X"])
def test_a_dead_program_process_is_never_ready(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: str) -> None:
    (tmp_path / recipe.PID).write_text("7")
    (tmp_path / recipe.SEEN).write_text("7 123")
    process = proc.Process(pid=7, ppid=1, pgid=7, sid=7, start_time=123, state=state, comm="tool", argv=("tool",))
    monkeypatch.setattr(recipe.proc, "processes", lambda root: {7: process})
    assert not recipe.alive(tmp_path)


@pytest.mark.feature("quick-local-command")
def test_a_program_cannot_claim_a_reused_pid_on_its_first_observation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / recipe.PID).write_text("7")
    process = proc.Process(pid=7, ppid=1, pgid=7, sid=7, start_time=123, state="S", comm="tool", argv=("tool",))
    monkeypatch.setattr(recipe.proc, "processes", lambda root: {7: process})
    assert not recipe.alive(tmp_path)
    assert not (tmp_path / recipe.SEEN).exists()


@pytest.mark.feature("quick-local-command")
def test_a_program_identity_is_recorded_before_the_first_observer(tmp_path: Path) -> None:
    recipe.record(tmp_path / recipe.PID, os.getpid())
    assert (tmp_path / recipe.PID).read_text() == str(os.getpid())
    seen = (tmp_path / recipe.SEEN).read_text()
    assert seen.startswith(f"{os.getpid()} ")
    assert recipe.alive(tmp_path)
    (tmp_path / recipe.SEEN).write_text(f"{os.getpid()} 0")
    assert not recipe.alive(tmp_path)


@pytest.mark.feature("quick-local-command")
def test_command_lookup_is_saved_from_the_selected_directory_and_path(tmp_path: Path) -> None:
    target = tmp_path / "target folder"
    target.mkdir()
    executable = target / "wrapper"
    executable.write_text("#!/usr/bin/env bash\nset -euo pipefail\n")
    executable.chmod(0o700)
    environ = {"PWD": str(target), "PATH": "."}
    assert recipe.recipes('["./wrapper","two words"]', [], ["claude"], environ) == [(str(executable), "two words")]
    assert recipe.recipes('["wrapper"]', [], ["claude"], environ) == [(str(executable),)]


@pytest.mark.feature("quick-local-command")
def test_master_context_reports_command_identity_and_capabilities(tmp_path: Path) -> None:
    swarm = _swarm(tmp_path, (TOOL, "--private-argument"))
    text = seeds.opening(member.Member(tmp_path, swarm, swarm.seats[0]))
    assert "--private-argument" not in text
    assert "program env" in text
    assert "agent hooks unavailable" in text
    assert "automatic handoff unavailable" in text
    assert "prompt typing unavailable" in text
    assert "outside automatic tiers" in text
    assert ", automatic tiers" not in text


@pytest.mark.feature("quick-local-command")
def test_the_real_launcher_delivers_literal_arguments_and_its_directory(tmp_path: Path) -> None:
    import subprocess

    directory = tmp_path / "work folder"
    directory.mkdir()
    result = tmp_path / "received.json"
    code = "import json, os, sys; from pathlib import Path; Path(sys.argv[1]).write_text(json.dumps([sys.argv[2:], os.getcwd(), os.getenv('AGENTIHOOKS_QUICK_SEAT')]))"
    arguments = ("two words", "$HOME;literal", "'quoted'", "*")
    launcher = tmp_path / "command.sh"
    launcher.write_text(
        recipe.script(
            (sys.executable, "-c", code, str(result), *arguments),
            {member.SEAT_ENV: "eng-1"},
            tmp_path / recipe.PID,
            str(directory),
        )
    )
    completed = subprocess.run(["bash", str(launcher)], capture_output=True, check=False, timeout=15)
    assert completed.returncode == 0, completed.stderr.decode()
    assert json.loads(result.read_text()) == [list(arguments), str(directory), "eng-1"]
    assert (tmp_path / recipe.SEEN).read_text().startswith((tmp_path / recipe.PID).read_text() + " ")
    assert not recipe.alive(tmp_path)


@pytest.mark.feature("quick-local-command")
@pytest.mark.parametrize(("live", "gone"), [(True, False), (False, False), (True, True)])
def test_a_forced_program_stop_targets_only_its_verified_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, live: bool, gone: bool
) -> None:
    (tmp_path / recipe.PID).write_text("7")
    calls: list[tuple[str, int]] = []

    def alive(folder: Path) -> bool:
        assert folder == tmp_path
        return live

    def send(descriptor: int, number: int) -> None:
        assert number == recipe.signal.SIGTERM
        calls.append(("signal", descriptor))
        if gone:
            raise ProcessLookupError

    monkeypatch.setattr(recipe.os, "pidfd_open", lambda pid: calls.append(("open", pid)) or 9)
    monkeypatch.setattr(recipe, "alive", alive)
    monkeypatch.setattr(recipe.signal, "pidfd_send_signal", send)
    monkeypatch.setattr(recipe.os, "close", lambda fd: calls.append(("close", fd)))
    assert recipe.stop(tmp_path) is live
    assert calls == [("open", 7), *([("signal", 9)] if live else []), ("close", 9)]


@pytest.mark.feature("quick-local-command")
def test_a_program_with_a_common_named_choice_needs_no_native_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts.quick import authentication

    monkeypatch.setattr("scripts.operator_env.fill", lambda env: None)
    assert authentication.selections("named:external", ["program"], {}) == [("default", "")]


@pytest.mark.feature("quick-local-command")
def test_a_command_option_keeps_the_up_subcommand() -> None:
    from scripts.quick import cli

    found = cli.parser().parse_args(["up", "--command", '["/usr/bin/env"]'])
    assert found.command == "up"
    assert found.command_argv == '["/usr/bin/env"]'


@pytest.mark.feature("quick-local-command")
def test_a_saved_executable_symlink_keeps_its_invocation_identity(tmp_path: Path) -> None:
    import subprocess

    executable = tmp_path / "selected python"
    executable.symlink_to(sys.executable)
    command = recipe.recipes(
        json.dumps([str(executable), "-c", "import sys; print(sys.executable)"]), [], ["program"], {}
    )[0]
    assert command[0] == str(executable)
    assert subprocess.check_output(command, text=True).strip() == str(executable)


@pytest.mark.feature("quick-local-command")
def test_program_exit_restores_a_shell_even_in_a_project_with_its_own_scripts_package(tmp_path: Path) -> None:
    import subprocess

    package = tmp_path / "scripts"
    package.mkdir()
    (package / "__init__.py").write_text("raise RuntimeError('project code must not run')")
    launcher = tmp_path / "command.sh"
    command = (sys.executable, "-c", "print('program-finished')")
    launcher.write_text(recipe.script(command, {}, tmp_path / recipe.PID, str(tmp_path)))
    completed = subprocess.run(
        ["bash", str(launcher)],
        input="printf 'shell-restored\\n'\nexit\n",
        text=True,
        capture_output=True,
        check=False,
        timeout=15,
    )
    assert completed.returncode == 0, completed.stderr
    assert "program-finished" in completed.stdout
    assert "shell-restored" in completed.stdout
    assert not recipe.alive(tmp_path)
    swarm = _swarm(tmp_path, command)
    calls: list[str] = []

    class Shell(Herdr):
        def bare(self, pane_id: str) -> bool:
            return True

    def retry(folder: Path, seat: str) -> str:
        calls.append(seat)
        return "started"

    def refuse_force(folder: Path, seat: str) -> str:
        pytest.fail("an exited program used forced relaunch")

    rows = tick.round_for(tmp_path, Shell(), retry, 10_000.0, refuse_force)
    assert rows[1] == {"seat": "eng-1", "outcome": "revived-started"}
    assert calls == ["eng-1"] and swarm.seats[1].command == command


@pytest.mark.feature("quick-local-command")
def test_the_program_runner_records_and_reaps_its_child(tmp_path: Path) -> None:
    code = recipe.run_program(tmp_path / recipe.PID, [sys.executable, "-c", "raise SystemExit(7)"])
    assert code == 7
    pid = (tmp_path / recipe.PID).read_text()
    assert (tmp_path / recipe.SEEN).read_text().startswith(pid + " ")
    assert not recipe.alive(tmp_path)


@pytest.mark.feature("quick-local-command")
@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("{", "a command must be a JSON list of arguments"),
        ("[]", "a command must be a non empty JSON list of non empty strings"),
        (json.dumps([chr(0)]), "a command argument may not hold a NUL or a newline"),
        (json.dumps([chr(10)]), "a command argument may not hold a NUL or a newline"),
    ],
)
def test_invalid_command_diagnostics_are_stable(text: str, message: str) -> None:
    with pytest.raises(QuickError) as refused:
        recipe.parse(text)
    assert str(refused.value) == message


@pytest.mark.feature("quick-local-command")
def test_override_boundaries_and_literal_equals_are_preserved() -> None:
    assert recipe.recipes(
        "", [f'1=["{TOOL}", "--tag=two words"]', f'3=["{TOOL}"]'], ["claude", "codex", "program"], {}
    ) == [(TOOL, "--tag=two words"), (), (TOOL,)]
    with pytest.raises(QuickError) as refused:
        recipe.recipes("", [f'3=["{TOOL}"]'], ["claude", "codex"], {})
    assert str(refused.value) == "--engineer-command needs N=<JSON list> with N from 1 to 2"
    with pytest.raises(QuickError) as refused:
        recipe.recipes("", [], ["program"], {})
    assert str(refused.value) == "a program engineer needs a command"


@pytest.mark.feature("quick-local-command")
@pytest.mark.parametrize("default_path", [True, False])
def test_executable_lookup_uses_each_path_entry_and_the_system_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, default_path: bool
) -> None:
    directory = tmp_path / "tools with spaces"
    directory.mkdir()
    tool = directory / "local-tool"
    tool.write_text("#!/usr/bin/env bash\nset -euo pipefail\n")
    tool.chmod(0o700)
    search = f"{tmp_path / 'missing'}{os.pathsep}{directory}"
    monkeypatch.setattr(recipe.os, "defpath", search)
    environ = {} if default_path else {"PATH": search}
    assert recipe.recipes('["local-tool"]', [], ["program"], environ) == [(str(tool),)]


@pytest.mark.feature("quick-local-command")
@pytest.mark.parametrize("directory", [True, False])
def test_a_directory_or_nonexecutable_file_is_not_a_command(tmp_path: Path, directory: bool) -> None:
    tool = tmp_path / "cannot-run"
    if directory:
        tool.mkdir()
    else:
        tool.write_text("data")
        tool.chmod(0o600)
    with pytest.raises(QuickError):
        recipe.recipes(json.dumps([str(tool)]), [], ["program"], {})


@pytest.mark.feature("quick-local-command")
@pytest.mark.parametrize(("saved_harness", "command"), [("claude", ()), ("codex", (TOOL,)), ("program", (TOOL,))])
def test_native_execution_never_adopts_another_kind_or_harness(
    tmp_path: Path, saved_harness: str, command: tuple[str, ...]
) -> None:
    swarm = _swarm(tmp_path, command, saved_harness)
    environ = launch.environ({}, tmp_path, swarm, swarm.seats[1])
    calls: list[tuple[str, list[str]]] = []

    def execute(binary: str, argv: list[str], child: Mapping[str, str]) -> None:
        calls.append((binary, argv))

    native = ["native-claude", "--model", "chosen"]
    recipe.execute_native("claude", native, environ, execvpe=execute)
    assert calls == [("native-claude", native)]


@pytest.mark.feature("quick-local-command")
@pytest.mark.parametrize("error", [None, "invalid-pid", "unavailable-process", "gone-process"])
def test_an_unavailable_process_handle_cannot_be_stopped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: str | None
) -> None:
    if error is not None:
        (tmp_path / recipe.PID).write_text("bad" if error == "invalid-pid" else "7")

    def unavailable(pid: int) -> int:
        if error == "gone-process":
            raise ProcessLookupError
        raise PermissionError

    monkeypatch.setattr(recipe.os, "pidfd_open", unavailable)
    assert recipe.stop(tmp_path) is (error == "gone-process")


@pytest.mark.feature("quick-local-command")
@pytest.mark.parametrize("available_at", [0, 2, 4, None])
def test_program_readiness_observes_the_process_within_its_poll_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, available_at: int | None
) -> None:
    swarm = _swarm(tmp_path, (TOOL,))
    calls: list[Path] = []
    waits: list[float] = []

    def alive(folder: Path) -> bool:
        calls.append(folder)
        return available_at is not None and len(calls) > available_at

    monkeypatch.setattr(launch, "SETTLE_S", 2.0)
    monkeypatch.setattr(launch, "POLL_S", 0.5)
    monkeypatch.setattr(recipe, "alive", alive)
    assert launch.program_ready(tmp_path, swarm.seats[1], sleep=waits.append) is (available_at is not None)
    waited = 4 if available_at is None else available_at
    assert waits == [0.5] * waited
    assert calls == [paths.seat_dir(tmp_path, "eng-1")] * (waited + 1)


@pytest.mark.feature("quick-local-command")
def test_program_readiness_rejects_a_native_seat(tmp_path: Path) -> None:
    seat = registry.Seat(2, "eng-1", "w1:p2", "claude", "native")
    with pytest.raises(AssertionError):
        launch.program_ready(tmp_path, seat, sleep=lambda seconds: pytest.fail("native process wait"))


@pytest.mark.feature("quick-local-command")
@pytest.mark.parametrize("harness", ["program", "claude"])
def test_force_selects_only_the_saved_occupant_control(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, harness: str
) -> None:
    swarm = _swarm(tmp_path, (TOOL,), harness)
    stopped: list[Path] = []
    ended: list[str] = []
    monkeypatch.setattr(recipe, "stop", lambda folder: stopped.append(folder) or True)
    monkeypatch.setattr(launch, "relaunch", lambda *args, **kwargs: "started")
    assert launch.force(tmp_path, "eng-1", {}, bare=lambda pane: False, end=ended.append) == "started"
    assert stopped == ([paths.seat_dir(tmp_path, "eng-1")] if harness == "program" else [])
    assert ended == ([] if harness == "program" else [swarm.seats[1].name])


@pytest.mark.feature("quick-local-command")
def test_force_refuses_a_busy_program_without_verified_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _swarm(tmp_path, (TOOL,))
    monkeypatch.setattr(recipe, "stop", lambda folder: False)
    assert (
        launch.force(
            tmp_path, "eng-1", {}, bare=lambda pane: False, end=lambda name: pytest.fail("program used native end")
        )
        == "unbound"
    )
    assert _seat(tmp_path).state == registry.PENDING


@pytest.mark.feature("quick-local-command")
@pytest.mark.parametrize("failure", [None, "herdr", "timeout"])
def test_live_herdr_runs_the_program_launcher_with_its_exact_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str | None
) -> None:
    import subprocess

    from scripts import herdr_host

    env = {"PATH": "/caller/tools", member.SEAT_ENV: "eng-1"}
    launcher = tmp_path / "command.sh"
    calls: list[tuple[str, Path, Mapping[str, str]]] = []

    def run(pane: str, script: Path, environ: Mapping[str, str]) -> None:
        calls.append((pane, script, environ))
        if failure == "herdr":
            raise herdr_host.HerdrError("unavailable")
        if failure == "timeout":
            raise subprocess.TimeoutExpired("herdr", 1)

    monkeypatch.setattr(herdr_host, "run", run)
    assert tick.LiveHerdr(env).run("w1:p2", launcher) is (failure is None)
    assert calls == [("w1:p2", launcher, env)]


@pytest.mark.feature("quick-local-command")
@pytest.mark.parametrize("route", ["claude-routed", "claude-bare", "claude-default", "codex-routed", "codex-default"])
def test_native_routing_executes_the_saved_wrapper_after_account_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, route: str
) -> None:
    from contextlib import ExitStack
    from types import SimpleNamespace

    from scripts import claude_quota_balancer, codex_router, deps_preflight, install, master_claude_login
    from scripts.hive import account_slots
    from scripts.routing import native_default

    harness = route.split("-")[0]
    swarm = _swarm(tmp_path, (TOOL, "two words"), harness)
    active = launch.environ(
        {
            "HOME": str(tmp_path),
            "AGENTIHOOKS_PROFILE_REPORT": "/private/profile",
            "AGENTIHOOKS_ROUTE_ACCOUNT": "stale-parent",
        },
        tmp_path,
        swarm,
        swarm.seats[1],
    )
    for name, value in active.items():
        monkeypatch.setenv(name, value)
    received: list[tuple[str, list[str], dict[str, str]]] = []
    admitted: list[tuple[str, str]] = []

    def execute(binary: str, arguments: list[str], environ: Mapping[str, str]) -> None:
        received.append((binary, arguments, dict(environ)))
        raise SystemExit(0)

    def admit(kind: str, account: str, environ: Mapping[str, str], run: Callable[[Mapping[str, str]], object]) -> None:
        admitted.append((kind, account))
        run(environ)

    def unavailable(*args: object, **kwargs: object) -> None:
        raise claude_quota_balancer.RoutingError("unavailable")

    monkeypatch.setattr(account_slots, "execute", admit)
    monkeypatch.setattr(install.os, "execvpe", execute)
    monkeypatch.setattr(install.shutil, "which", lambda name: f"native-{name}")
    monkeypatch.setattr(install, "_load_claude_runtime_env", lambda: None)
    monkeypatch.setattr(install, "_write_route_report", lambda *args, **kwargs: None)
    monkeypatch.setattr(install, "_claude_command", lambda binary, args: [binary, *args])
    monkeypatch.setattr(master_claude_login, "_token_lock", lambda env: ExitStack())
    monkeypatch.setattr(deps_preflight, "ensure", lambda: None)
    monkeypatch.setattr(deps_preflight, "refresh_codex_wrapper", lambda name: None)
    monkeypatch.setattr(claude_quota_balancer, "route_requires_fable", lambda *args: False)
    monkeypatch.setattr(claude_quota_balancer, "forced", lambda env, account: SimpleNamespace(account="alpha"))
    monkeypatch.setattr(claude_quota_balancer, "select_launch_credential", unavailable)
    monkeypatch.setattr(
        claude_quota_balancer,
        "launch_environment",
        lambda decision, env: {**env, "AGENTIHOOKS_ROUTE_ACCOUNT": decision.account},
    )
    monkeypatch.setattr(native_default, "prepare", lambda kind, env: {**env, "AGENTIHOOKS_ROUTE_ACCOUNT": "default"})
    monkeypatch.setattr(native_default.native_login, "check", lambda *args: None)
    monkeypatch.setattr(codex_router, "_own_command", lambda *args: None)
    monkeypatch.setattr(codex_router.codex_sessions, "resolve", lambda args, env: args)
    monkeypatch.setattr(codex_router, "identity_warnings", lambda env: [])
    monkeypatch.setattr(codex_router, "_route", lambda *args: (codex_router.CodexAccount("alpha"), "forced", 0, "?"))
    monkeypatch.setattr(codex_router, "credential", lambda account, env: account)
    monkeypatch.setattr(codex_router, "default_signed_in", lambda *args: True)
    monkeypatch.setattr(codex_router, "_report", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        codex_router, "child_environment", lambda account, env: {**env, "AGENTIHOOKS_ROUTE_ACCOUNT": account.name}
    )
    monkeypatch.setattr(codex_router, "command", lambda account, binary, args: [binary, *args])
    arguments = ["--model", "chosen", "opening prompt"]
    with pytest.raises(SystemExit) as stopped:
        if route == "claude-default":
            native_default.launch_claude("native-claude", arguments, active, "")
        elif route.startswith("codex"):
            selected = "native-default" if route == "codex-default" else "alpha"
            codex_router.main(["--route", selected, *arguments], active, execvpe=execute)
        else:
            flags = ["--agentihooks-fallback-bare"] if route == "claude-bare" else ["--route", "alpha"]
            install.cmd_claude([*flags, *arguments])
    assert stopped.value.code == 0
    [(binary, forwarded, child)] = received
    assert binary == TOOL and forwarded == [TOOL, "two words", *arguments]
    assert admitted == [
        (harness, "default" if route.endswith("default") else "unrouted" if route.endswith("bare") else "alpha")
    ]
    assert child[recipe.PROMPT_ENV] == str(paths.opening(tmp_path, "eng-1"))
    assert child["AGENTIHOOKS_PROFILE_REPORT"] == "/private/profile"
    assert child[member.SEAT_ENV] == "eng-1"
    assert child.get("AGENTIHOOKS_ROUTE_ACCOUNT") == (
        "default" if route.endswith("default") else None if route.endswith("bare") else "alpha"
    )


@pytest.mark.feature("quick-local-command")
def test_initial_program_readiness_waits_for_its_process_without_native_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    swarm = _swarm(tmp_path, (TOOL,))
    checks: list[Path] = []
    waits: list[float] = []
    monkeypatch.setattr(tick, "LiveHerdr", Panes(write_pid=False))
    monkeypatch.setattr(launch, "time", SimpleNamespace(sleep=waits.append))
    monkeypatch.setattr(launch, "_recorded", lambda *args: pytest.fail("program waited for native binding"))

    def alive(folder: Path) -> bool:
        checks.append(folder)
        return len(checks) >= 3

    monkeypatch.setattr(recipe, "alive", alive)
    assert launch.start_all(tmp_path, swarm, {}) == {"eng-1": 0}
    assert _seat(tmp_path).state == registry.READY
    assert waits == [launch.POLL_S, launch.POLL_S]
    assert checks == [paths.seat_dir(tmp_path, "eng-1")] * 3
