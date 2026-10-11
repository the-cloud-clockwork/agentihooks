import json
import os
from pathlib import Path

import pytest

from scripts.swarm_v2.herdr import session
from scripts.swarm_v2.supervision import LaunchRefused

pytestmark = pytest.mark.unit
EXE = "exe-" + "a" * 32


def attempt_files(tmp_path, principal=f"{EXE}\n"):
    attempts = tmp_path / "attempts"
    attempt = attempts / EXE
    (attempt / "homes" / "codex").mkdir(parents=True)
    authority = {
        "execution_id": EXE,
        "generation": 1,
        "task_id": "fixture",
        "seat_id": "eng-1",
        "swarm_id": "fixture",
        "grant_id": "lgr-" + "b" * 32,
    }
    (attempt / "execution.json").write_text(json.dumps({"attempt": EXE, "homes": {"codex": "homes/codex"}}))
    (attempt / "registration.json").write_text(json.dumps(authority))
    launch = tmp_path / "launch.json"
    launch.write_text(json.dumps({"schema_version": 1, "authority": authority, "harness": "codex", "agent": ["true"]}))
    material = tmp_path / "material"
    material.mkdir()
    (material / "principals").write_text(principal)
    return material, attempts, launch


def expected(attempts: Path, **kept) -> dict:
    home = attempts.resolve() / EXE
    return {
        **kept,
        "SHELL": "/bin/bash",
        "HOME": str(home / "homes" / "codex"),
        "CLAUDE_CONFIG_DIR": str(home / "homes" / "codex" / ".claude"),
        "CODEX_HOME": str(home / "homes" / "codex" / ".codex"),
        "XDG_RUNTIME_DIR": str(home / "run"),
        "TMPDIR": str(home / "tmp"),
    }


def test_the_session_runs_in_the_supervised_herdr_environment(tmp_path):
    material, attempts, launch = attempt_files(tmp_path)
    environ = {
        "PATH": "/usr/bin:/bin",
        "TERM": "xterm",
        "LANG": "C.UTF-8",
        "USER": "worker",
        "LOGNAME": "worker",
        "HOME": "/home/worker",
        "SSH_ORIGINAL_COMMAND": "herdr status",
        "LD_PRELOAD": "/x.so",
        "HERDR_SOCKET": "/elsewhere",
    }
    found = session.environment(material, attempts, launch, environ)
    assert found == expected(
        attempts, TERM="xterm", PATH="/usr/bin:/bin", LANG="C.UTF-8", USER="worker", LOGNAME="worker"
    )
    assert list(found)[:6] == ["TERM", "PATH", "LANG", "USER", "LOGNAME", "SHELL"]


def test_a_session_without_a_terminal_type_is_dumb(tmp_path):
    material, attempts, launch = attempt_files(tmp_path)
    assert session.environment(material, attempts, launch, {}) == expected(attempts, TERM="dumb")


@pytest.mark.parametrize("principal", ["exe-other\n", f"{EXE}x\n", "", f"../{EXE}\n"])
def test_the_principal_must_be_one_execution_id(tmp_path, principal):
    material, attempts, launch = attempt_files(tmp_path, principal)
    with pytest.raises(LaunchRefused) as caught:
        session.environment(material, attempts, launch, {})
    assert str(caught.value) == "terminal principal is not an execution id"


def test_main_execs_the_forced_command_in_bash(tmp_path, monkeypatch):
    material, attempts, launch = attempt_files(tmp_path)
    calls = []
    monkeypatch.setattr(session.os, "execve", lambda *args: calls.append(args))
    argv = ["--material", str(material), "--attempts", str(attempts), "--launch", str(launch)]
    environ = {"SSH_ORIGINAL_COMMAND": "herdr status server --json"}
    assert session.main(argv, environ) == 1
    assert calls == [("/bin/bash", ["/bin/bash", "-c", "herdr status server --json"], expected(attempts, TERM="dumb"))]


def test_main_without_a_command_opens_a_login_shell(tmp_path, monkeypatch):
    material, attempts, launch = attempt_files(tmp_path)
    calls = []
    monkeypatch.setattr(session.os, "execve", lambda *args: calls.append(args))
    argv = ["--material", str(material), "--attempts", str(attempts), "--launch", str(launch)]
    session.main(argv, {"SSH_ORIGINAL_COMMAND": ""})
    assert calls[0][1] == ["/bin/bash", "-l"]


def test_main_reads_the_pod_paths_and_the_process_environment(monkeypatch):
    seen = []
    monkeypatch.setattr(session, "environment", lambda *args: seen.append(args) or {"A": "1"})
    monkeypatch.setattr(session.os, "execve", lambda *args: seen.append(args))
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", "true")
    session.main([])
    assert seen[0][:3] == (
        Path("/var/run/swarm/terminal"),
        Path("/home/worker/attempts"),
        Path("/var/run/swarm/launch/launch.json"),
    )
    assert seen[0][3] == dict(os.environ)
    assert seen[1] == ("/bin/bash", ["/bin/bash", "-c", "true"], {"A": "1"})


def test_a_refused_launch_opens_no_shell(tmp_path, monkeypatch, capsys):
    material, attempts, launch = attempt_files(tmp_path, "nobody\n")
    calls = []
    monkeypatch.setattr(session.os, "execve", lambda *args: calls.append(args))
    argv = ["--material", str(material), "--attempts", str(attempts), "--launch", str(launch)]
    assert session.main(argv, {"SSH_ORIGINAL_COMMAND": "true"}) == 1
    assert (
        capsys.readouterr().err
        == "terminal session refused: LaunchRefused: terminal principal is not an execution id\n"
    )
    assert calls == []


def test_missing_terminal_material_opens_no_shell(tmp_path, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(session.os, "execve", lambda *args: calls.append(args))
    argv = ["--material", str(tmp_path / "absent"), "--attempts", str(tmp_path), "--launch", str(tmp_path / "x")]
    assert session.main(argv, {}) == 1
    assert capsys.readouterr().err.startswith("terminal session refused: FileNotFoundError: ")
    assert calls == []


def test_an_attempt_record_without_its_attempt_opens_no_shell(tmp_path, monkeypatch, capsys):
    material, attempts, launch = attempt_files(tmp_path)
    (attempts / EXE / "execution.json").write_text("{}")
    monkeypatch.setattr(session.os, "execve", lambda *args: pytest.fail("no shell"))
    argv = ["--material", str(material), "--attempts", str(attempts), "--launch", str(launch)]
    assert session.main(argv, {}) == 1
    assert capsys.readouterr().err == "terminal session refused: KeyError: 'attempt'\n"
