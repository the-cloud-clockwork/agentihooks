import json
import os
import subprocess
from pathlib import Path

import pytest

import scripts.swarm_v2.image_probe as image_probe

pytestmark = pytest.mark.unit
STATUS = {"running": True, "protocol": 22, "capabilities": {"health_check": True}}
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


def done(stdout=""):
    return subprocess.CompletedProcess([], 0, stdout, "")


class Commands:
    def __init__(self, answers=None):
        self.calls, self.answers = [], answers or {}

    def __call__(self, command, environ, timeout, cwd=None):
        self.calls.append({"command": command, "environ": environ, "timeout": timeout, "cwd": cwd})
        answer = self.answers.get(" ".join(command[1:]), done(f"{command[0]} 1.0"))
        if isinstance(answer, list):
            answer = answer.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer


class Server:
    def __init__(self):
        self.terminated, self.waited, self.started = False, None, []

    def terminate(self):
        self.terminated = True

    def wait(self, timeout):
        self.waited = timeout


@pytest.fixture
def server(monkeypatch):
    instance = Server()

    def popen(command, **kwargs):
        instance.started.append((command, kwargs))
        return instance

    monkeypatch.setattr(image_probe.subprocess, "Popen", popen)
    monkeypatch.setattr(image_probe, "STARTUP_SECONDS", 0.0)
    return instance


def ticking(step=1.0):
    now = [0.0]

    def clock():
        now[0] += step
        return now[0]

    return clock


def test_run_executes_detached_from_stdin_with_captured_text(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(image_probe.subprocess, "run", lambda command, **kwargs: seen.append((command, kwargs)) or 7)

    assert image_probe.run(["x", "y"], {"A": "1"}, 5, cwd=tmp_path) == 7
    assert image_probe.run(["z"], {}, 3) == 7

    assert seen == [
        (
            ["x", "y"],
            {
                "env": {"A": "1"},
                "cwd": tmp_path,
                "stdin": subprocess.DEVNULL,
                "capture_output": True,
                "text": True,
                "timeout": 5,
            },
        ),
        (
            ["z"],
            {"env": {}, "cwd": None, "stdin": subprocess.DEVNULL, "capture_output": True, "text": True, "timeout": 3},
        ),
    ]


def test_version_reads_the_stripped_version_line(monkeypatch):
    commands = Commands({"--version": done(" claude 1\n")})
    monkeypatch.setattr(image_probe, "run", commands)

    assert image_probe.version("claude", {"H": "1"}) == "claude 1"
    assert commands.calls == [{"command": ["claude", "--version"], "environ": {"H": "1"}, "timeout": 30, "cwd": None}]


def test_methods_lists_every_request_method_sorted():
    assert image_probe.methods(SCHEMA) == ["ping", "workspace.list"]


@pytest.mark.parametrize("schema", [{}, {"schemas": {}}, {"schemas": {"request": {}}}])
def test_methods_of_a_schema_without_requests_is_empty(schema):
    assert image_probe.methods(schema) == []


@pytest.mark.parametrize(
    "request_schema",
    [{}, {"properties": {}}, {"properties": {"method": {}}}, {"properties": {"method": {"const": 3}}}],
)
def test_methods_skip_requests_without_a_method_name(request_schema):
    schema = {"schemas": {"request": {"oneOf": [request_schema, {"properties": {"method": {"const": "pane.read"}}}]}}}

    assert image_probe.methods(schema) == ["pane.read"]


@pytest.mark.parametrize(("text", "document"), [('{"a": 1}', {"a": 1}), ("", {}), ("not json", {}), ("[1]", {})])
def test_parsed_reads_only_a_json_object(text, document):
    assert image_probe.parsed(text) == document


def test_server_status_polls_until_the_server_runs(monkeypatch):
    slow = subprocess.TimeoutExpired(["herdr"], 10)
    answers = [done("not json"), slow, done('{"running": false}'), done(json.dumps(STATUS))]
    commands = Commands({"status server --json": answers})
    monkeypatch.setattr(image_probe, "run", commands)
    slept = []

    status = image_probe.server_status({"HOME": "/h"}, clock=ticking(), sleep=slept.append)

    assert status == STATUS
    assert slept == [0.2, 0.2, 0.2]
    assert (
        commands.calls
        == [{"command": ["herdr", "status", "server", "--json"], "environ": {"HOME": "/h"}, "timeout": 10, "cwd": None}]
        * 4
    )


def test_server_status_stops_at_its_deadline(monkeypatch):
    monkeypatch.setattr(image_probe, "run", Commands({"status server --json": done('{"running": false}')}))
    slept = []

    status = image_probe.server_status({}, clock=ticking(5.0), sleep=slept.append)

    assert status == {"running": False}
    assert len(slept) == 3


def test_server_status_returns_at_once_when_running(monkeypatch):
    monkeypatch.setattr(image_probe, "run", Commands({"status server --json": done(json.dumps(STATUS))}))
    slept = []

    assert image_probe.server_status({}, clock=ticking(100.0), sleep=slept.append) == STATUS
    assert slept == []


def test_herdr_runs_a_private_headless_server_and_reads_its_capabilities(tmp_path, monkeypatch, server):
    commands = Commands(
        {
            "status server --json": done(json.dumps(STATUS)),
            "api schema --json": done(json.dumps(SCHEMA)),
            "--version": done("herdr 0.9.1\n"),
        }
    )
    monkeypatch.setattr(image_probe, "run", commands)
    root = tmp_path / "attempts" / "herdr"

    observed = image_probe.herdr(root, {"PATH": "/bin"})

    assert observed == {
        "version": "herdr 0.9.1",
        "status": STATUS,
        "schema": {"protocol": 22, "methods": ["ping", "workspace.list"]},
    }
    environ = {
        "PATH": "/bin",
        "HOME": str(root),
        "XDG_RUNTIME_DIR": str(root / "tmp"),
        "HERDR_CONFIG_PATH": str(root / "herdr.toml"),
    }
    devnull = subprocess.DEVNULL
    assert server.started == [
        (["herdr", "server"], {"env": environ, "stdin": devnull, "stdout": devnull, "stderr": devnull})
    ]
    assert (root / "tmp").stat().st_mode & 0o777 == 0o700
    assert server.terminated and server.waited == 10
    assert [(call["command"], call["timeout"]) for call in commands.calls] == [
        (["herdr", "status", "server", "--json"], 10),
        (["herdr", "api", "schema", "--json"], 30),
        (["herdr", "--version"], 30),
    ]
    assert all(call["environ"] == environ for call in commands.calls)


def test_herdr_stops_its_server_when_the_schema_read_fails(tmp_path, monkeypatch, server):
    commands = Commands({"status server --json": done(json.dumps(STATUS)), "api schema --json": OSError("gone")})
    monkeypatch.setattr(image_probe, "run", commands)

    with pytest.raises(OSError):
        image_probe.herdr(tmp_path / "herdr", {})

    assert server.terminated and server.waited == 10


def test_herdr_without_a_schema_reports_no_methods(tmp_path, monkeypatch, server):
    commands = Commands({"status server --json": done(json.dumps(STATUS)), "api schema --json": done("")})
    monkeypatch.setattr(image_probe, "run", commands)

    assert image_probe.herdr(tmp_path / "herdr", {})["schema"] == {"protocol": None, "methods": []}


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


@pytest.mark.parametrize(
    ("name", "command", "timeout"),
    [
        ("claude", ["claude", "-p", "hello"], 60),
        ("codex", ["codex", "exec", "--skip-git-repo-check", "--dangerously-bypass-hook-trust", "hello"], 20),
    ],
)
def test_harness_launches_headless_in_a_git_work_folder(tmp_path, monkeypatch, name, command, timeout):
    home = tmp_path / "homes" / name
    write_sessions(home, 1)
    commands = Commands({"--version": done("v 1\n")})
    monkeypatch.setattr(image_probe, "run", commands)

    observed = image_probe.harness(name, tmp_path, {"PATH": "/bin"})

    assert observed == {"version": "v 1", "hook_registrations": 1}
    launched = {"PATH": "/bin", "HOME": str(home)}
    assert commands.calls == [
        {
            "command": ["git", "init", "-q", str(tmp_path / "work")],
            "environ": {"PATH": "/bin"},
            "timeout": 30,
            "cwd": None,
        },
        {"command": command, "environ": launched, "timeout": timeout, "cwd": tmp_path / "work"},
        {"command": [name, "--version"], "environ": launched, "timeout": 30, "cwd": None},
    ]


def test_a_harness_launch_timeout_still_reports_its_hooks(tmp_path, monkeypatch):
    write_sessions(tmp_path / "homes/claude", 1)
    timeout = subprocess.TimeoutExpired(["claude"], 60)
    monkeypatch.setattr(image_probe, "run", Commands({"-p hello": timeout, "--version": done("c\n")}))

    assert image_probe.harness("claude", tmp_path, {}) == {"version": "c", "hook_registrations": 1}


def test_probe_bootstraps_an_attempt_and_observes_every_target(tmp_path, monkeypatch):
    requests, launched = [], []
    monkeypatch.setattr(image_probe, "bootstrap", requests.append)
    monkeypatch.setattr(image_probe, "herdr", lambda root, environ: {"root": str(root), "environ": environ})
    monkeypatch.setattr(
        image_probe, "harness", lambda name, attempt, environ: launched.append((name, attempt, environ)) or name
    )
    manifest = tmp_path / "manifest.json"
    manifest.write_text('{"source_revision": "x"}')
    monkeypatch.setattr(image_probe, "MANIFEST", manifest)
    root = tmp_path / "attempts"
    root.mkdir(mode=0o755)

    found = image_probe.probe(tmp_path / "templates", root, {"PATH": "/bin", "HERDR_SOCKET": "/s"})

    [request] = requests
    assert request.root == root and request.attempt == "image-probe" and request.templates == tmp_path / "templates"
    assert request.profiles == {"claude": "fixture-claude", "codex": "fixture-codex"}
    assert request.accounts == {"claude": "AH_CC_TOKEN_FIXTURE", "codex": "AH_CX_TOKEN_FIXTURE"}
    assert request.endpoints == {"AGENTIHOOKS_LEDGER_URL": "https://ledger.swarm.invalid:8765"}
    assert request.interpreter == Path("/opt/venv/bin/python")
    assert (request.uid, request.gid) == (os.getuid(), os.getgid())
    environ = {"PATH": "/bin", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}
    assert launched == [("claude", root / "image-probe", environ), ("codex", root / "image-probe", environ)]
    assert found == {
        "manifest": {"source_revision": "x"},
        "observed": {
            "herdr": {"root": str(root / "herdr"), "environ": environ},
            "claude": "claude",
            "codex": "codex",
        },
    }


def test_probe_creates_a_private_attempt_root(tmp_path, monkeypatch):
    monkeypatch.setattr(image_probe, "bootstrap", lambda request: None)
    monkeypatch.setattr(image_probe, "herdr", lambda root, environ: {})
    monkeypatch.setattr(image_probe, "harness", lambda name, attempt, environ: {})
    manifest = tmp_path / "manifest.json"
    manifest.write_text("{}")
    monkeypatch.setattr(image_probe, "MANIFEST", manifest)

    image_probe.probe(tmp_path, tmp_path / "attempts", {})

    assert (tmp_path / "attempts").stat().st_mode & 0o777 == 0o700


def test_main_prints_the_probe_of_the_image_paths(monkeypatch, capsys):
    seen = []
    monkeypatch.setattr(image_probe, "probe", lambda *args: seen.append(args) or {"b": 1, "a": 2})
    monkeypatch.setenv("PROBE_MARK", "1")

    assert image_probe.main() == 0

    assert capsys.readouterr().out == '{"a": 2, "b": 1}\n'
    [(templates, root, environ)] = seen
    assert (templates, root) == (Path("/opt/probe/profiles"), Path("/home/worker/attempts"))
    assert environ == dict(os.environ) and type(environ) is dict
