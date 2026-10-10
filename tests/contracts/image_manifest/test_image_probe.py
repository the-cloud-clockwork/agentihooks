import json
import subprocess
from pathlib import Path

import pytest
import scripts.swarm_v2.image_probe as image_probe

pytestmark = pytest.mark.unit
STATUS = {"running": True, "protocol": 22, "capabilities": {"detached_server_daemon": True}}
SCHEMA = {
    "protocol": 22,
    "schemas": {
        "request": {
            "oneOf": [
                {"properties": {"method": {"const": "workspace.list"}}},
                {"properties": {"method": {"const": "ping"}}},
            ]
        }
    },
}


def done(stdout="", code=0):
    return subprocess.CompletedProcess([], code, stdout, "")


class Commands:
    def __init__(self, answers=None):
        self.calls, self.answers = [], answers or {}

    def __call__(self, command, environ, timeout, cwd=None):
        self.calls.append({"command": command, "environ": environ, "timeout": timeout, "cwd": cwd})
        answer = self.answers.get(" ".join(command[1:]), done(f"{command[0]} 1.0"))
        if isinstance(answer, list):
            return answer.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer


class Server:
    def __init__(self):
        self.terminated = self.waited = False

    def terminate(self):
        self.terminated = True

    def wait(self, timeout):
        self.waited = timeout


@pytest.fixture
def server(monkeypatch):
    started, instance = [], Server()

    def popen(command, **kwargs):
        started.append((command, kwargs))
        return instance

    monkeypatch.setattr(image_probe.subprocess, "Popen", popen)
    instance.started = started
    return instance


def test_methods_lists_every_request_method_sorted():
    assert image_probe.methods(SCHEMA) == ["ping", "workspace.list"]


@pytest.mark.parametrize("schema", [{}, {"schemas": {}}, {"schemas": {"request": {}}}])
def test_methods_of_a_schema_without_requests_is_empty(schema):
    assert image_probe.methods(schema) == []


def test_server_status_polls_until_the_server_runs(monkeypatch):
    commands = Commands(
        {"status server --json": [done("not json"), done('{"running": false}'), done(json.dumps(STATUS))]}
    )
    monkeypatch.setattr(image_probe, "run", commands)
    slept = []

    status = image_probe.server_status({"HOME": "/h"}, clock=lambda: 0.0, sleep=slept.append)

    assert status == STATUS
    assert slept == [image_probe.POLL_SECONDS, image_probe.POLL_SECONDS]
    assert [call["command"] for call in commands.calls] == [["herdr", "status", "server", "--json"]] * 3
    assert {call["timeout"] for call in commands.calls} == {10}
    assert commands.calls[0]["environ"] == {"HOME": "/h"}


def test_server_status_stops_at_its_deadline(monkeypatch):
    monkeypatch.setattr(image_probe, "run", Commands({"status server --json": done('{"running": false}')}))
    times = iter([0.0, 5.0, image_probe.STARTUP_SECONDS])
    slept = []

    status = image_probe.server_status({}, clock=lambda: next(times), sleep=slept.append)

    assert status == {"running": False}
    assert len(slept) == 1


def test_herdr_runs_a_private_headless_server_and_reads_its_capabilities(tmp_path, monkeypatch, server):
    commands = Commands(
        {
            "status server --json": done(json.dumps(STATUS)),
            "api schema --json": done(json.dumps(SCHEMA)),
            "--version": done("herdr 0.9.1\n"),
        }
    )
    monkeypatch.setattr(image_probe, "run", commands)

    observed = image_probe.herdr(tmp_path / "herdr", {"PATH": "/bin"})

    assert observed == {
        "version": "herdr 0.9.1",
        "status": STATUS,
        "schema": {"protocol": 22, "methods": ["ping", "workspace.list"]},
    }
    [(command, kwargs)] = server.started
    assert command == ["herdr", "server"]
    assert kwargs["env"] == {
        "PATH": "/bin",
        "HOME": str(tmp_path / "herdr"),
        "XDG_RUNTIME_DIR": str(tmp_path / "herdr/tmp"),
        "HERDR_CONFIG_PATH": str(tmp_path / "herdr/herdr.toml"),
    }
    assert kwargs["stdin"] is kwargs["stdout"] is kwargs["stderr"] is subprocess.DEVNULL
    assert (tmp_path / "herdr/tmp").stat().st_mode & 0o777 == 0o700
    assert server.terminated and server.waited == 10
    assert all(call["environ"] == kwargs["env"] for call in commands.calls)


def test_herdr_stops_its_server_when_the_schema_read_fails(tmp_path, monkeypatch, server):
    commands = Commands({"status server --json": done(json.dumps(STATUS)), "api schema --json": OSError("gone")})
    monkeypatch.setattr(image_probe, "run", commands)

    with pytest.raises(OSError):
        image_probe.herdr(tmp_path, {})

    assert server.terminated and server.waited == 10


def test_herdr_without_a_schema_reports_no_methods(tmp_path, monkeypatch, server):
    commands = Commands({"status server --json": done(json.dumps(STATUS)), "api schema --json": done("")})
    monkeypatch.setattr(image_probe, "run", commands)

    assert image_probe.herdr(tmp_path, {})["schema"] == {"protocol": None, "methods": []}


def write_sessions(home: Path, count: int):
    (home / ".agentihooks").mkdir(parents=True)
    sessions = {f"s{n}": {"cwd": "/w"} for n in range(count)}
    (home / ".agentihooks/active-sessions.json").write_text(json.dumps(sessions))


def test_registrations_count_sessions_the_hook_registered(tmp_path):
    write_sessions(tmp_path, 2)

    assert image_probe.registrations(tmp_path) == 2


@pytest.mark.parametrize("body", [None, "not json"])
def test_registrations_without_a_readable_record_are_zero(tmp_path, body):
    if body is not None:
        (tmp_path / ".agentihooks").mkdir()
        (tmp_path / ".agentihooks/active-sessions.json").write_text(body)

    assert image_probe.registrations(tmp_path) == 0


@pytest.mark.parametrize("name", ["claude", "codex"])
def test_harness_launches_headless_in_a_git_work_folder(tmp_path, monkeypatch, name):
    home = tmp_path / "homes" / name
    write_sessions(home, 1)
    commands = Commands({"--version": done("v 1\n")})
    monkeypatch.setattr(image_probe, "run", commands)

    observed = image_probe.harness(name, tmp_path, {"PATH": "/bin"})

    command, timeout = image_probe.LAUNCHES[name]
    assert observed == {"version": "v 1", "hook_registrations": 1}
    init, launch, version = commands.calls
    assert init["command"] == ["git", "init", "-q", str(tmp_path / "work")]
    assert launch == {
        "command": command,
        "environ": {"PATH": "/bin", "HOME": str(home)},
        "timeout": timeout,
        "cwd": tmp_path / "work",
    }
    assert version["command"] == [name, "--version"]


def test_a_harness_launch_timeout_still_reports_its_hooks(tmp_path, monkeypatch):
    write_sessions(tmp_path / "homes/claude", 1)
    timeout = subprocess.TimeoutExpired(["claude"], 60)
    monkeypatch.setattr(image_probe, "run", Commands({"-p hello": timeout, "--version": done("c\n")}))

    assert image_probe.harness("claude", tmp_path, {}) == {"version": "c", "hook_registrations": 1}


def test_probe_bootstraps_an_attempt_and_observes_every_target(tmp_path, monkeypatch):
    requests = []
    monkeypatch.setattr(image_probe, "bootstrap", requests.append)
    monkeypatch.setattr(image_probe, "herdr", lambda root, environ: {"root": str(root), "environ": environ})
    monkeypatch.setattr(image_probe, "harness", lambda name, attempt, environ: {"attempt": str(attempt)})
    manifest = tmp_path / "manifest.json"
    manifest.write_text('{"source_revision": "x"}')
    monkeypatch.setattr(image_probe, "MANIFEST", manifest)
    root = tmp_path / "attempts"

    found = image_probe.probe(tmp_path / "templates", root, {"PATH": "/bin", "HERDR_SOCKET": "/s"})

    [request] = requests
    assert (request.root, request.attempt, request.templates) == (root, "image-probe", tmp_path / "templates")
    assert request.profiles == image_probe.PROFILES and request.accounts == image_probe.ACCOUNTS
    assert request.endpoints == image_probe.ENDPOINTS
    assert request.interpreter == image_probe.INTERPRETER
    environ = {"PATH": "/bin", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}
    assert found == {
        "manifest": {"source_revision": "x"},
        "observed": {
            "herdr": {"root": str(root / "herdr"), "environ": environ},
            "claude": {"attempt": str(root / "image-probe")},
            "codex": {"attempt": str(root / "image-probe")},
        },
    }
    assert root.stat().st_mode & 0o777 == 0o700


def test_main_prints_the_probe(monkeypatch, capsys, tmp_path):
    seen = []
    monkeypatch.setattr(image_probe, "probe", lambda *args: seen.append(args) or {"observed": {}})

    assert image_probe.main(["--templates", str(tmp_path), "--root", str(tmp_path / "r")]) == 0

    assert json.loads(capsys.readouterr().out) == {"observed": {}}
    assert seen[0][:2] == (tmp_path, tmp_path / "r")
