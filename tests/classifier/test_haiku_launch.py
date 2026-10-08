import json
import os
import shlex
import shutil
import signal
import time
from pathlib import Path
from subprocess import PIPE, Popen
from subprocess import run as _real_run
from types import SimpleNamespace

import pytest

from hooks.classifier import YesNo, fallbacks
from hooks.classifier.errors import BackendFailure
from hooks.classifier.result import DecisionRequest
from scripts.install import _write_route_report

REQUEST = DecisionRequest("typo", {"simple": YesNo("Small?", true="yes", false="no")})
RAW = {"answers": {"simple": {"noul": 1}}}
ROUTED = "[agenti] account=other routing_left=70% 5h_left=80% 7d_left=70% source=cached\n"


def _launcher(seen, report=None, code=0, stdout=ROUTED + json.dumps({"structured_output": RAW}) + "\n"):
    def run(args, **kwargs):
        seen.append((args, kwargs))
        assert json.loads((Path(kwargs["cwd"]) / "request.json").read_text()) == REQUEST.wire()
        if report is not None:
            Path(args[args.index("--agentihooks-report") + 1]).write_text(report)
        return SimpleNamespace(returncode=code, stdout=stdout)

    return run


@pytest.mark.parametrize("route", [None, "routed"])
@pytest.mark.parametrize("parent_token", [None, "parent-placeholder"])
def test_haiku_launches_through_agentihooks_claude(monkeypatch, tmp_path, route, parent_token):
    launcher = tmp_path / "agentihooks"
    launcher.write_text("#!/bin/sh\n")
    launcher.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN")
    if parent_token:
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", parent_token)
    if route:
        monkeypatch.setenv("AGENTIHOOKS_ROUTE_ACCOUNT", route)
    seen = []
    monkeypatch.setattr(fallbacks.subprocess, "run", _launcher(seen))
    assert fallbacks.ClaudeCliBackend().decide(REQUEST).source == "haiku"
    args, kwargs = seen[0]
    wire = Path(kwargs["cwd"]) / "request.json"
    assert args[:3] == ["bash", "-lic", f'{fallbacks.STOP_STARTUP_AGENT}exec "$0" "$@" < {wire}']
    assert args[3:6] == [str(launcher), "claude", "--agentihooks-report"]
    assert args[6] == str(Path(kwargs["cwd"]) / "route")
    assert args[7 : args.index("-p")] == (["--route", route] if route else [])
    assert args[args.index("-p") :][:3] == ["-p", "--model", "haiku"]
    assert args[args.index("--system-prompt") + 1] == fallbacks.PROMPT
    assert kwargs["env"].get("CLAUDE_CODE_OAUTH_TOKEN") == os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")


def test_launcher_off_path_is_left_to_the_shell(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path))
    seen = []
    monkeypatch.setattr(fallbacks.subprocess, "run", _launcher(seen))
    fallbacks.ClaudeCliBackend().decide(REQUEST)
    assert seen[0][0][3] == "agentihooks"


def test_unroutable_account_fails_naming_why(monkeypatch, tmp_path):
    seen = []
    written = tmp_path / "route"
    _write_route_report(str(written), status="failed", error="no account with routing_left=0% has room")
    monkeypatch.setattr(fallbacks.subprocess, "run", _launcher(seen, report=written.read_text(), code=3, stdout=""))
    with pytest.raises(BackendFailure, match="^no Claude account: no account with routing_left=0% has room$"):
        fallbacks.ClaudeCliBackend().decide(REQUEST)
    assert len(seen) == 1


@pytest.mark.parametrize("report", [None, "status=routed\naccount=other\nplacement=open\n"])
def test_failure_without_route_refusal_keeps_cli_status(monkeypatch, report):
    monkeypatch.setattr(fallbacks.subprocess, "run", _launcher([], report=report, code=1, stdout=""))
    with pytest.raises(BackendFailure, match="^CLI status 1$"):
        fallbacks.ClaudeCliBackend().decide(REQUEST)


def test_shell_startup_reading_stdin_leaves_the_request(monkeypatch, tmp_path):
    (tmp_path / ".bash_profile").write_text("cat > /dev/null\n")
    launcher = tmp_path / "agentihooks"
    launcher.write_text(
        '#!/bin/sh\ncat > "$(dirname "$0")/stdin.json"\necho "[agenti] account=stub"\n'
        f"echo '{json.dumps({'structured_output': RAW})}'\n"
    )
    launcher.chmod(0o755)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PATH", f"{tmp_path}:/usr/bin:/bin")
    monkeypatch.setattr(fallbacks.subprocess, "run", _real_run)
    assert fallbacks.ClaudeCliBackend().decide(REQUEST).source == "haiku"
    assert json.loads((tmp_path / "stdin.json").read_text()) == REQUEST.wire()


def _shell_home(monkeypatch, tmp_path, profile):
    (tmp_path / ".bash_profile").write_text(profile)
    (tmp_path / "ssh-agent").symlink_to(shutil.which("sleep"))
    launcher = tmp_path / "agentihooks"
    launcher.write_text(f"#!/bin/sh\necho '{json.dumps({'structured_output': RAW})}'\n")
    launcher.chmod(0o755)
    monkeypatch.delenv("SSH_AGENT_PID", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PATH", f"{tmp_path}:/usr/bin:/bin")
    monkeypatch.setattr(fallbacks.subprocess, "run", _real_run)


def _gone(pid, wait=5.0):
    deadline = time.monotonic() + wait
    while True:
        try:
            state = Path(f"/proc/{pid}/stat").read_text().rpartition(")")[2].split()[0]
        except FileNotFoundError:
            return True
        if state in {"Z", "X"}:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


@pytest.fixture
def earlier_agent(tmp_path):
    binary = tmp_path / "earlier" / "ssh-agent"
    binary.parent.mkdir()
    binary.symlink_to(shutil.which("sleep"))
    agent = Popen([str(binary), "300"])
    # Process start times count in clock ticks; the shell must start at least one tick later.
    time.sleep(0.05)
    yield agent
    agent.kill()
    agent.wait()


STARTS_AGENT = (
    'ssh-agent 300 >/dev/null 2>&1 &\nSSH_AGENT_PID=$!; export SSH_AGENT_PID\necho "$!" > "$HOME/agent.pid"\n'
)
needs_proc = pytest.mark.skipif(not Path("/proc/self/stat").is_file(), reason="process start times come from /proc")


@needs_proc
def test_shell_startup_agent_is_stopped_before_the_cli(monkeypatch, tmp_path, earlier_agent):
    _shell_home(monkeypatch, tmp_path, STARTS_AGENT)
    monkeypatch.setenv("SSH_AGENT_PID", str(earlier_agent.pid))
    assert fallbacks.ClaudeCliBackend().decide(REQUEST).source == "haiku"
    pid = int((tmp_path / "agent.pid").read_text())
    try:
        assert _gone(pid)
    finally:
        if not _gone(pid, wait=0):
            os.kill(pid, signal.SIGKILL)
    assert not _gone(earlier_agent.pid, wait=0.3)


@needs_proc
@pytest.mark.parametrize("inherited", [True, False])
def test_agent_started_before_the_shell_survives_the_cli(monkeypatch, tmp_path, earlier_agent, inherited):
    attach = "" if inherited else f"SSH_AGENT_PID={earlier_agent.pid}; export SSH_AGENT_PID\n"
    _shell_home(monkeypatch, tmp_path, attach)
    if inherited:
        monkeypatch.setenv("SSH_AGENT_PID", str(earlier_agent.pid))
    assert fallbacks.ClaudeCliBackend().decide(REQUEST).source == "haiku"
    assert not _gone(earlier_agent.pid, wait=0.3)


@needs_proc
def test_startup_process_that_is_no_agent_survives_the_cli(monkeypatch, tmp_path):
    _shell_home(monkeypatch, tmp_path, STARTS_AGENT.replace("ssh-agent 300", "sleep 300"))
    assert fallbacks.ClaudeCliBackend().decide(REQUEST).source == "haiku"
    pid = int((tmp_path / "agent.pid").read_text())
    try:
        assert not _gone(pid, wait=0.3)
    finally:
        if not _gone(pid, wait=0):
            os.kill(pid, signal.SIGKILL)


@needs_proc
def test_caller_value_naming_a_later_agent_survives(tmp_path):
    binary = tmp_path / "ssh-agent"
    binary.symlink_to(shutil.which("sleep"))
    shell = Popen(["bash"], stdin=PIPE, text=True)
    time.sleep(0.05)
    agent = Popen([str(binary), "300"])
    try:
        # exec keeps the shell's pid and start time but gives it a caller environment naming the later agent.
        script = f"exec env SSH_AGENT_PID={agent.pid} bash -c {shlex.quote(fallbacks.STOP_STARTUP_AGENT)}\n"
        shell.communicate(script, timeout=10)
        assert not _gone(agent.pid, wait=0.3)
    finally:
        shell.kill()
        shell.wait()
        agent.kill()
        agent.wait()


def test_codex_keeps_native_auth_environment(monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN")

    def run(args, **kwargs):
        assert "CLAUDE_CODE_OAUTH_TOKEN" not in kwargs["env"]
        Path(args[args.index("--output-last-message") + 1]).write_text(json.dumps(RAW))
        return SimpleNamespace(returncode=0, stdout="")

    monkeypatch.setattr(fallbacks.subprocess, "run", run)
    assert fallbacks.CodexCliBackend().decide(REQUEST).source == "luna"
