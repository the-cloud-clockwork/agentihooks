import contextlib
import json
import os
import shutil
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from hooks.proc import _process
from scripts.swarm_v2 import worker_health
from scripts.swarm_v2.supervision_protocol import write
from scripts.swarm_v2.worker_health import Probe, evaluate, main

SAMPLE = "-".join(("fixture", "bearer"))
SLEEP = shutil.which("sleep")
HEALTHY_HERDR = """#!/bin/sh
printf '%s\\n' "$*" "$HOME" "$XDG_RUNTIME_DIR" "$HERDR_CONFIG_PATH" "$CODEX_HOME" "$CLAUDE_CONFIG_DIR" "${HERDR_SOCKET:-}" > "$PROBE_LOG"
echo '{"result": []}'
"""


def executable(path: Path, text: str) -> Path:
    path.write_text(text)
    path.chmod(0o755)
    return path


def fixture(tmp_path, harness="codex"):
    attempt = tmp_path / "attempt"
    home = attempt / "homes" / harness
    native = home / (".codex" if harness == "codex" else ".claude")
    native.mkdir(parents=True)
    for folder in ("run", "tmp"):
        (attempt / folder).mkdir()
    (attempt / "execution.json").write_text(json.dumps({"attempt": "exe-1", "homes": {harness: f"homes/{harness}"}}))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    hook = executable(bin_dir / "hook", "#!/bin/sh\n")
    document = {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": f"{hook} --event start"}]}]}}
    (native / ("hooks.json" if harness == "codex" else "settings.json")).write_text(json.dumps(document))
    executable(bin_dir / "herdr", HEALTHY_HERDR)
    executable(bin_dir / harness, "#!/bin/sh\n")
    root = attempt / "run" / "supervision" / "incarnation"
    root.mkdir(parents=True)
    scope = {
        "authority": {"execution_id": "exe-1"},
        "incarnation": root.name,
        "supervisor_pid": os.getpid(),
        "process_namespace": os.readlink("/proc/self/ns/pid"),
    }
    write(root / "context.json", scope)
    write(root / "running.json", {**scope, "pane_id": "p", "status": "running"})
    environ = {"PATH": str(bin_dir), "PROBE_LOG": str(tmp_path / "herdr.log"), "HERDR_SOCKET": "outer"}
    return attempt, root, environ


class Brain(BaseHTTPRequestHandler):
    status = 200
    delay = 0.0
    seen = []

    def do_GET(self):
        time.sleep(Brain.delay)
        Brain.seen.append((self.path, self.headers.get("Authorization")))
        self.send_response(Brain.status)
        self.end_headers()

    def log_message(self, *_args):
        pass


@pytest.fixture
def brain():
    Brain.status = 200
    Brain.delay = 0.0
    Brain.seen = []
    server = HTTPServer(("127.0.0.1", 0), Brain)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


def closed_port() -> str:
    server = HTTPServer(("127.0.0.1", 0), Brain)
    port = server.server_address[1]
    server.server_close()
    return f"http://127.0.0.1:{port}"


def tree(path: Path) -> dict:
    return {str(p.relative_to(path)): p.read_bytes() if p.is_file() else None for p in sorted(path.rglob("*"))}


def report(attempt, environ, mode, harness="codex"):
    return evaluate(Probe(attempt, harness, environ), mode)


def test_healthy_runtime_is_ready_with_every_check_passing(tmp_path, brain):
    attempt, root, environ = fixture(tmp_path)
    environ["BRAIN_URL"] = f"http://127.0.0.1:{brain.server_address[1]}/"
    environ["BRAIN_HTTP_TOKEN"] = SAMPLE
    result = report(attempt, environ, "readiness")
    assert result == {
        "mode": "readiness",
        "status": "ready",
        "harness": "codex",
        "attempt": str(attempt),
        "incarnation": "incarnation",
        "checks": {
            "binaries": None,
            "home": None,
            "runtime_paths": None,
            "hook": None,
            "supervisor": None,
            "herdr": None,
            "agent": None,
        },
        "dependencies": {"brain": "ok"},
        "worker_startup_failure_reason": None,
    }
    assert Brain.seen == [("/health", f"Bearer {SAMPLE}")]
    home = attempt / "homes" / "codex"
    assert (tmp_path / "herdr.log").read_text().splitlines() == [
        "workspace list",
        str(home),
        str(attempt / "tmp"),
        str(root / "herdr.toml"),
        str(home / ".codex"),
        str(home / ".claude"),
        "",
    ]


def test_claude_harness_reads_its_settings_hook(tmp_path):
    attempt, _, environ = fixture(tmp_path, harness="claude")
    assert report(attempt, environ, "startup", harness="claude")["status"] == "ready"


def test_brain_outage_keeps_liveness_and_reports_degraded_readiness(tmp_path):
    attempt, _, environ = fixture(tmp_path)
    environ["BRAIN_URL"] = closed_port()
    live = report(attempt, environ, "liveness")
    assert live == {
        "mode": "liveness",
        "status": "live",
        "harness": "codex",
        "attempt": str(attempt),
        "incarnation": "incarnation",
        "checks": {"supervisor": None},
        "worker_startup_failure_reason": None,
    }
    ready = report(attempt, environ, "readiness")
    assert ready["status"] == "degraded"
    assert ready["dependencies"] == {"brain": "brain_unreachable"}
    assert ready["worker_startup_failure_reason"] is None


def test_brain_http_error_names_its_status(tmp_path, brain):
    attempt, _, environ = fixture(tmp_path)
    Brain.status = 503
    environ["BRAIN_URL"] = f"http://127.0.0.1:{brain.server_address[1]}"
    environ["KB_ROUTER_TOKEN"] = SAMPLE
    result = report(attempt, environ, "startup")
    assert result["status"] == "degraded"
    assert result["dependencies"] == {"brain": "brain_http_503"}
    assert Brain.seen == [("/health", f"Bearer {SAMPLE}")]


def test_brain_without_token_sends_no_authorization(tmp_path, brain):
    attempt, _, environ = fixture(tmp_path)
    environ["BRAIN_URL"] = f"http://127.0.0.1:{brain.server_address[1]}"
    assert report(attempt, environ, "readiness")["dependencies"] == {"brain": "ok"}
    assert Brain.seen == [("/health", None)]


@pytest.mark.parametrize("url", ["", "   "])
def test_unconfigured_brain_is_not_a_degradation(tmp_path, url):
    attempt, _, environ = fixture(tmp_path)
    environ["BRAIN_URL"] = url
    result = report(attempt, environ, "readiness")
    assert result["status"] == "ready"
    assert result["dependencies"] == {"brain": "unconfigured"}


def test_invalid_brain_url_is_unreachable(tmp_path):
    attempt, _, environ = fixture(tmp_path)
    environ["BRAIN_URL"] = "not a url"
    assert report(attempt, environ, "readiness")["dependencies"] == {"brain": "brain_unreachable"}


def test_brain_url_with_an_invalid_port_is_unreachable(tmp_path):
    attempt, _, environ = fixture(tmp_path)
    environ["BRAIN_URL"] = "http://127.0.0.1:notaport"
    assert report(attempt, environ, "readiness")["dependencies"] == {"brain": "brain_unreachable"}


def test_recovered_brain_clears_degradation_without_touching_the_attempt(tmp_path, brain):
    attempt, _, environ = fixture(tmp_path)
    port = brain.server_address[1]
    brain.shutdown()
    brain.server_close()
    environ["BRAIN_URL"] = f"http://127.0.0.1:{port}"
    before = tree(attempt)
    assert report(attempt, environ, "readiness")["status"] == "degraded"
    restored = HTTPServer(("127.0.0.1", port), Brain)
    thread = threading.Thread(target=restored.serve_forever, daemon=True)
    thread.start()
    try:
        result = report(attempt, environ, "readiness")
    finally:
        restored.shutdown()
        restored.server_close()
    assert result["status"] == "ready"
    assert result["dependencies"] == {"brain": "ok"}
    assert tree(attempt) == before


@pytest.mark.parametrize("name", ["herdr", "codex"])
def test_missing_binary_never_reports_ready(tmp_path, name):
    attempt, _, environ = fixture(tmp_path)
    (tmp_path / "bin" / name).unlink()
    for mode in ("startup", "readiness"):
        result = report(attempt, environ, mode)
        assert result["status"] == "not_ready"
        assert result["checks"]["binaries"] == f"missing_binary:{name}"
        assert result["worker_startup_failure_reason"] == f"missing_binary:{name}"


def test_missing_path_uses_no_host_binaries(tmp_path):
    attempt, _, environ = fixture(tmp_path)
    del environ["PATH"]
    assert report(attempt, environ, "startup")["checks"]["binaries"] == "missing_binary:herdr"


def bare_hook(attempt):
    path = attempt / "homes" / "codex" / ".codex" / "hooks.json"
    path.write_text(json.dumps({"hooks": {"SessionStart": [{"hooks": [{"command": "hook --event start"}]}]}}))


def test_bare_hook_name_resolves_on_the_probe_path(tmp_path):
    attempt, _, environ = fixture(tmp_path)
    bare_hook(attempt)
    assert report(attempt, environ, "startup")["checks"]["hook"] is None


def test_missing_path_ignores_host_path_and_working_directory(tmp_path, monkeypatch):
    attempt, _, environ = fixture(tmp_path)
    bare_hook(attempt)
    decoy = tmp_path / "XXXX"
    decoy.mkdir()
    for name in ("herdr", "codex", "hook"):
        executable(decoy / name, "#!/bin/sh\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    del environ["PATH"]
    checks = report(attempt, environ, "startup")["checks"]
    assert (checks["binaries"], checks["hook"]) == ("missing_binary:herdr", "hook_uncallable")


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses file modes")
@pytest.mark.parametrize(
    "mode,reason", [(0o555, "home_unwritable"), (0o000, "home_unreadable"), (0o300, "home_unreadable")]
)
def test_unusable_private_home_never_reports_ready(tmp_path, mode, reason):
    attempt, _, environ = fixture(tmp_path)
    home = attempt / "homes" / "codex"
    home.chmod(mode)
    try:
        result = report(attempt, environ, "readiness")
    finally:
        home.chmod(0o700)
    assert result["status"] == "not_ready"
    assert result["checks"]["home"] == reason
    assert result["worker_startup_failure_reason"] == reason


@pytest.mark.parametrize(
    "record",
    [
        {},
        {"homes": []},
        {"homes": {"codex": 3}},
        {"homes": {"codex": "../outside"}},
        {"homes": {"codex": "homes/none"}},
    ],
)
def test_unresolvable_private_home_is_missing(tmp_path, record):
    attempt, _, environ = fixture(tmp_path)
    (tmp_path / "outside").mkdir()
    (attempt / "execution.json").write_text(json.dumps(record))
    result = report(attempt, environ, "startup")
    assert result["checks"]["home"] == "home_missing"
    assert result["checks"]["hook"] == "hook_missing"


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses file modes")
@pytest.mark.parametrize("folder", ["run", "tmp"])
def test_unwritable_runtime_path_never_reports_ready(tmp_path, folder):
    attempt, _, environ = fixture(tmp_path)
    (attempt / folder).chmod(0o555)
    try:
        result = report(attempt, environ, "startup")
    finally:
        (attempt / folder).chmod(0o700)
    assert result["status"] == "not_ready"
    assert result["checks"]["runtime_paths"] == f"runtime_path_unwritable:{folder}"


def test_missing_runtime_path_is_unwritable(tmp_path):
    attempt, _, environ = fixture(tmp_path)
    (attempt / "tmp").rmdir()
    assert report(attempt, environ, "startup")["checks"]["runtime_paths"] == "runtime_path_unwritable:tmp"


@pytest.mark.parametrize(
    "hooks,reason",
    [
        ({}, "hook_missing"),
        ({"SessionStart": []}, "hook_missing"),
        ({"SessionStart": [{"hooks": [{"command": 3}]}]}, "hook_missing"),
        ({"SessionStart": ["bad", {"hooks": "bad"}]}, "hook_missing"),
        ({"SessionStart": [{"hooks": [{"command": "'unterminated"}]}]}, "hook_invalid"),
        ({"SessionStart": [{"hooks": [{"command": "  "}]}]}, "hook_invalid"),
        ({"SessionStart": [{"hooks": [{"command": "/absent/hook start"}]}]}, "hook_uncallable"),
        ([], "hook_missing"),
    ],
)
def test_session_start_hook_must_be_callable(tmp_path, hooks, reason):
    attempt, _, environ = fixture(tmp_path)
    (attempt / "homes" / "codex" / ".codex" / "hooks.json").write_text(json.dumps({"hooks": hooks}))
    result = report(attempt, environ, "startup")
    assert result["status"] == "not_ready"
    assert result["checks"]["hook"] == reason


def test_every_session_start_hook_is_checked(tmp_path):
    attempt, _, environ = fixture(tmp_path)
    path = attempt / "homes" / "codex" / ".codex" / "hooks.json"
    document = json.loads(path.read_text())
    document["hooks"]["SessionStart"].append({"hooks": [{"command": "absent-hook"}]})
    path.write_text(json.dumps(document))
    assert report(attempt, environ, "startup")["checks"]["hook"] == "hook_uncallable"


def hook_commands(attempt, commands):
    path = attempt / "homes" / "codex" / ".codex" / "hooks.json"
    path.write_text(json.dumps({"hooks": {"SessionStart": [{"hooks": [{"command": c} for c in commands]}]}}))


def test_shell_hook_commands_resolve_each_program(tmp_path):
    attempt, _, environ = fixture(tmp_path)
    (tmp_path / "a+b").mkdir()
    hook_commands(
        attempt,
        [
            f"cd {tmp_path}/a+b && hook -m hooks",
            "FIXTURE=1 OTHER=2 hook start",
            f'"{tmp_path}/bin/hook" --event start',
            "hook | hook; hook || hook",
        ],
    )
    assert report(attempt, environ, "startup")["checks"]["hook"] is None


def test_relative_hook_program_resolves_against_its_cd_folder(tmp_path, monkeypatch):
    attempt, _, environ = fixture(tmp_path)
    executable(tmp_path / "bin" / "local-hook", "#!/bin/sh\n")
    executable(tmp_path / "local-hook", "#!/bin/sh\n")
    monkeypatch.chdir(tmp_path)
    hook_commands(attempt, ["cd bin && ./local-hook"])
    assert report(attempt, environ, "startup")["checks"]["hook"] is None
    monkeypatch.setenv("HOME", str(tmp_path))
    hook_commands(attempt, ["cd ~/bin && ./local-hook", "~/bin/local-hook start"])
    assert report(attempt, environ, "startup")["checks"]["hook"] is None
    hook_commands(attempt, ["cd {tmp_path}/attempt && ./local-hook".format(tmp_path=tmp_path)])
    assert report(attempt, environ, "startup")["checks"]["hook"] == "hook_uncallable"


@pytest.mark.parametrize(
    "command",
    ["cd {tmp}/absent && hook", "cd {tmp} {tmp} && hook", "cd && hook", "hook && absent-hook", "FIXTURE=1", "hook &&"],
)
def test_shell_hook_with_an_uncallable_part_is_uncallable(tmp_path, command):
    attempt, _, environ = fixture(tmp_path)
    hook_commands(attempt, [command.format(tmp=tmp_path)])
    assert report(attempt, environ, "startup")["checks"]["hook"] == "hook_uncallable"


def test_herdr_failure_is_not_death(tmp_path):
    attempt, _, environ = fixture(tmp_path)
    executable(tmp_path / "bin" / "herdr", "#!/bin/sh\necho '{}'\nexit 1\n")
    assert report(attempt, environ, "liveness")["status"] == "live"
    result = report(attempt, environ, "startup")
    assert result["status"] == "not_ready"
    assert result["checks"]["herdr"] == "herdr_unavailable"
    assert result["worker_startup_failure_reason"] == "herdr_unavailable"


@pytest.mark.parametrize("output", ["not json", "[]"])
def test_herdr_protocol_answer_must_be_an_object(tmp_path, output):
    attempt, _, environ = fixture(tmp_path)
    executable(tmp_path / "bin" / "herdr", f"#!/bin/sh\necho '{output}'\n")
    assert report(attempt, environ, "startup")["checks"]["herdr"] == "herdr_unavailable"


def test_hung_herdr_is_bounded(tmp_path, monkeypatch):
    attempt, _, environ = fixture(tmp_path)
    executable(tmp_path / "bin" / "herdr", f"#!/bin/sh\nexec {SLEEP} 5\n")
    assert evaluate(Probe(attempt, "codex", environ, herdr_timeout=0.2), "startup")["checks"]["herdr"] == (
        "herdr_unavailable"
    )


def test_cli_probe_thresholds_are_arguments(tmp_path, capsys, monkeypatch, brain):
    attempt, _, environ = fixture(tmp_path)
    executable(tmp_path / "bin" / "herdr", f"#!/bin/sh\n{SLEEP} 0.6\necho '{{}}'\n")
    Brain.delay = 0.6
    environ["BRAIN_URL"] = f"http://127.0.0.1:{brain.server_address[1]}"
    base = ["startup", "--attempt", str(attempt), "--harness", "codex"]
    code, out = run_main([*base, "--herdr-timeout", "0.2", "--brain-timeout", "0.2"], capsys, monkeypatch, environ)
    tight = json.loads(out.out)
    assert (code, tight["checks"]["herdr"], tight["dependencies"]) == (
        1,
        "herdr_unavailable",
        {"brain": "brain_unreachable"},
    )
    code, out = run_main(base, capsys, monkeypatch, environ)
    assert (code, json.loads(out.out)["status"]) == (0, "ready")


def test_cli_probe_thresholds_default_to_two_seconds():
    args = worker_health.build_parser().parse_args(["startup", "--attempt", "a", "--harness", "codex"])
    assert (args.herdr_timeout, args.brain_timeout) == (2.0, 2.0)


def test_herdr_probe_reads_no_input_and_is_bounded(tmp_path, monkeypatch):
    attempt, _, environ = fixture(tmp_path)
    seen = []
    run = subprocess.run

    def spy(*args, **kwargs):
        seen.append({key: kwargs.get(key) for key in ("stdin", "capture_output", "timeout", "check")})
        return run(*args, **kwargs)

    monkeypatch.setattr(worker_health.subprocess, "run", spy)
    assert report(attempt, environ, "startup")["checks"]["herdr"] is None
    assert seen == [
        {"stdin": subprocess.DEVNULL, "capture_output": True, "timeout": worker_health.HERDR_TIMEOUT, "check": True}
    ]


def test_brain_probe_trims_the_url_and_is_bounded(tmp_path, monkeypatch):
    attempt, _, environ = fixture(tmp_path)
    environ.update(BRAIN_URL=" http://brain.test/X// ", BRAIN_HTTP_TOKEN=SAMPLE)
    seen = []

    def urlopen(request, timeout=None):
        seen.append((request.full_url, request.get_header("Authorization"), timeout))
        return contextlib.nullcontext()

    monkeypatch.setattr(worker_health.urllib.request, "urlopen", urlopen)
    assert report(attempt, environ, "readiness")["dependencies"] == {"brain": "ok"}
    assert seen == [("http://brain.test/X/health", f"Bearer {SAMPLE}", worker_health.BRAIN_TIMEOUT)]


def dead_pid() -> int:
    child = subprocess.Popen(["true"])
    child.wait()
    return child.pid


@pytest.mark.parametrize(
    "change",
    [
        {"supervisor_pid": "dead"},
        {"supervisor_pid": "text"},
        {"supervisor_pid": True},
        {"process_namespace": "pid:[1]"},
    ],
)
def test_supervisor_absent_fails_liveness_and_readiness(tmp_path, change):
    attempt, root, environ = fixture(tmp_path)
    scope = json.loads((root / "context.json").read_text())
    scope.update({k: dead_pid() if v == "dead" else v for k, v in change.items()})
    write(root / "context.json", scope)
    live = report(attempt, environ, "liveness")
    assert live["status"] == "not_live"
    assert live["incarnation"] is None
    assert live["checks"] == {"supervisor": "supervisor_absent"}
    assert live["worker_startup_failure_reason"] == "supervisor_absent"
    ready = report(attempt, environ, "readiness")
    assert ready["status"] == "not_ready"
    assert ready["checks"]["herdr"] == "herdr_unavailable"
    assert ready["checks"]["agent"] == "agent_not_running"


def test_finished_incarnation_is_not_live(tmp_path):
    attempt, root, environ = fixture(tmp_path)
    write(root / "result.json", {"reason": "agent_completed"})
    assert report(attempt, environ, "liveness")["checks"] == {"supervisor": "supervisor_absent"}


def test_zombie_supervisor_is_not_live(tmp_path, monkeypatch):
    attempt, root, environ = fixture(tmp_path)
    scope = json.loads((root / "context.json").read_text())
    child = subprocess.Popen(["true"])
    try:
        deadline = time.monotonic() + 5
        while _process(child.pid, Path("/proc")).state != "Z" and time.monotonic() < deadline:
            time.sleep(0.01)
        write(root / "context.json", {**scope, "supervisor_pid": child.pid})
        monkeypatch.setattr(worker_health.Path, "readlink", lambda _path: Path(scope["process_namespace"]))
        assert report(attempt, environ, "liveness")["checks"] == {"supervisor": "supervisor_absent"}
    finally:
        child.wait()


def test_unreadable_supervisor_namespace_is_not_live(tmp_path, monkeypatch):
    attempt, _, environ = fixture(tmp_path)

    def refuse(_path):
        raise PermissionError

    monkeypatch.setattr(worker_health.Path, "readlink", refuse)
    assert report(attempt, environ, "liveness")["status"] == "not_live"


def test_startup_names_an_absent_supervisor(tmp_path):
    attempt, root, environ = fixture(tmp_path)
    write(root / "result.json", {})
    checks = report(attempt, environ, "startup")["checks"]
    assert (checks["supervisor"], checks["herdr"]) == ("supervisor_absent", "herdr_unavailable")


def test_only_the_newest_incarnation_counts(tmp_path):
    attempt, root, environ = fixture(tmp_path)
    older = root.parent / "older"
    older.mkdir()
    scope = {**json.loads((root / "context.json").read_text()), "incarnation": "older"}
    write(older / "context.json", scope)
    os.utime(older / "context.json", (1, 1))
    assert report(attempt, environ, "liveness")["incarnation"] == "incarnation"
    newer = root.parent / "newer"
    newer.mkdir()
    write(newer / "context.json", {**scope, "incarnation": "newer", "supervisor_pid": dead_pid()})
    assert report(attempt, environ, "liveness")["checks"] == {"supervisor": "supervisor_absent"}


def test_agent_not_running_blocks_readiness_only(tmp_path):
    attempt, root, environ = fixture(tmp_path)
    (root / "running.json").unlink()
    assert report(attempt, environ, "startup")["status"] == "ready"
    result = report(attempt, environ, "readiness")
    assert result["status"] == "not_ready"
    assert result["checks"]["agent"] == "agent_not_running"
    assert result["worker_startup_failure_reason"] == "agent_not_running"


def test_running_record_from_another_incarnation_is_ignored(tmp_path):
    attempt, root, environ = fixture(tmp_path)
    write(root / "running.json", {"incarnation": "other", "status": "running"})
    assert report(attempt, environ, "readiness")["checks"]["agent"] == "agent_not_running"


def test_first_failing_check_is_the_startup_failure_reason(tmp_path):
    attempt, root, environ = fixture(tmp_path)
    (tmp_path / "bin" / "codex").unlink()
    (root / "running.json").unlink()
    (attempt / "homes" / "codex" / ".codex" / "hooks.json").unlink()
    result = report(attempt, environ, "readiness")
    assert result["checks"]["hook"] == "hook_missing"
    assert result["worker_startup_failure_reason"] == "missing_binary:codex"


def test_diagnose_reports_names_never_values(tmp_path):
    attempt, _, environ = fixture(tmp_path)
    (tmp_path / "bin" / "codex").unlink()
    environ.update({"BRAIN_URL": closed_port(), "FIXTURE_PRIVATE": SAMPLE, "bad name": "x"})
    result = report(attempt, environ, "diagnose")
    assert result["status"] == "not_ready"
    assert result["dependencies"] == {"brain": "brain_unreachable"}
    assert result["worker_startup_failure_reason"] == "missing_binary:codex"
    assert result["environment"] == sorted(["BRAIN_URL", "FIXTURE_PRIVATE", "HERDR_SOCKET", "PATH", "PROBE_LOG"])
    assert SAMPLE not in json.dumps(result)


def run_main(args, capsys, monkeypatch, environ):
    code = main(args, environ)
    return code, capsys.readouterr()


def test_cli_defaults_to_the_process_arguments_and_environment(tmp_path, capsys, monkeypatch):
    attempt, _, environ = fixture(tmp_path)
    for name, value in environ.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("BRAIN_URL", raising=False)
    monkeypatch.setattr("sys.argv", ["health.py", "readiness", "--attempt", str(attempt), "--harness", "codex"])
    assert main() == 0
    out = capsys.readouterr().out
    result = json.loads(out)
    assert (result["mode"], result["status"]) == ("readiness", "ready")
    assert out == json.dumps(result, sort_keys=True) + "\n"


def test_cli_exit_codes_follow_status(tmp_path, capsys, monkeypatch):
    attempt, root, environ = fixture(tmp_path)
    environ["BRAIN_URL"] = closed_port()
    base = ["--attempt", str(attempt), "--harness", "codex"]
    code, out = run_main(["readiness", *base], capsys, monkeypatch, environ)
    assert code == 0
    assert json.loads(out.out)["status"] == "degraded"
    assert run_main(["liveness", *base], capsys, monkeypatch, environ)[0] == 0
    (root / "running.json").unlink()
    code, out = run_main(["readiness", *base], capsys, monkeypatch, environ)
    assert code == 1
    assert json.loads(out.out)["checks"]["agent"] == "agent_not_running"
    assert run_main(["diagnose", *base], capsys, monkeypatch, environ)[0] == 0
    write(root / "result.json", {})
    assert run_main(["liveness", *base], capsys, monkeypatch, environ)[0] == 1


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["ready", "--attempt", "a", "--harness", "codex"],
        ["startup", "--attempt", "a", "--harness", "copilot"],
        ["startup", "--harness", "codex"],
        ["startup", "--attempt", "a"],
    ],
)
def test_cli_usage_errors_exit_64(args, capsys, monkeypatch):
    code, out = run_main(args, capsys, monkeypatch, {})
    assert code == 64
    assert out.out == ""


def test_cli_help_exits_zero(capsys, monkeypatch):
    code, out = run_main(["--help"], capsys, monkeypatch, {})
    assert code == 0
    assert out.out.startswith("usage: health.py")
