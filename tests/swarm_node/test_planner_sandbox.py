from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import Mock

import pytest

from scripts.swarm_v2 import worker_image


@pytest.mark.feature("codex-planner-native-sandbox")
def test_worker_image_locks_the_planner_sandbox_runtime() -> None:
    root = Path(__file__).resolve().parents[2]
    tools = worker_image.load_lock(root / "docker/swarm-node/versions.lock", "amd64")["tools"]
    sandbox = tools["bwrap"]
    assert sandbox["companion"] == "codex"
    assert sandbox["version"] == tools["codex"]["version"]
    assert sandbox["url"] == tools["codex"]["url"].replace("/codex-", "/bwrap-")
    assert sandbox["member"] == tools["codex"]["member"].replace("codex-", "bwrap-")
    assert sandbox["sha256"] != tools["codex"]["sha256"]


@pytest.mark.feature("codex-planner-native-sandbox")
@pytest.mark.parametrize("name", ["bwrap", "codex-code-mode-host"])
def test_companion_identity_accepts_its_native_usage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    binary = tmp_path / name
    binary.write_bytes(b"locked companion")
    usage = f"usage: {binary} [OPTIONS...] [--] COMMAND [ARGS...]" if name == "bwrap" else f"Usage: {name} [OPTIONS]"
    run = Mock(return_value=CompletedProcess([], 0, usage + "\n", ""))
    monkeypatch.setattr(worker_image.subprocess, "run", run)
    tools = {"tools": {name: {"companion": "codex", "version": "0.162.0"}}}
    assert worker_image.tool_versions(tools, tmp_path)[name] == (
        f"{name} 0.162.0 sha256:{worker_image.checksum(binary)}"
    )
    run.assert_called_once_with([str(binary), "--help"], check=True, capture_output=True, text=True)


@pytest.mark.feature("codex-planner-native-sandbox")
@pytest.mark.parametrize("name", ["bwrap", "codex-code-mode-host"])
def test_companion_identity_refuses_another_executable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    binary = tmp_path / name
    binary.write_bytes(b"wrong companion")
    run = Mock(return_value=CompletedProcess([], 0, "usage: other [OPTIONS]\n", ""))
    monkeypatch.setattr(worker_image.subprocess, "run", run)
    tools = {"tools": {name: {"companion": "codex", "version": "0.162.0"}}}
    with pytest.raises(ValueError, match=f"binary usage mismatch: {name}"):
        worker_image.tool_versions(tools, tmp_path)


@pytest.mark.feature("codex-planner-native-sandbox")
@pytest.mark.parametrize("failure", ["", "namespace", "repository", "report"])
def test_native_qualification_requires_command_report_and_write_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    from tests.swarm_node import planner_sandbox_probe

    calls = []

    def run(command: list[str], **kwargs: object) -> CompletedProcess[str]:
        calls.append((command, kwargs))
        target = Path(command[-1])
        code = 0
        if failure == "namespace":
            code = 1
        elif target.name == "forbidden":
            code = 0 if failure == "repository" else 1
            if not code:
                target.write_text("validated")
        elif target.suffix == ".json" and failure != "report":
            target.write_text("validated")
        return CompletedProcess(command, code, "", "namespace denied" if failure == "namespace" else "")

    monkeypatch.setattr(planner_sandbox_probe.subprocess, "run", run)
    report = planner_sandbox_probe.qualify(tmp_path)
    assert report["passed"] is (not failure)
    assert len(calls) == 3
    for command, options in calls:
        assert command[command.index("--permission-profile") + 1] == "planner"
        assert "danger-full-access" not in command
        assert 'extends=":read-only"' in command[2]
        assert options["timeout"] == 30
        assert options["capture_output"] is True
        assert Path(options["env"]["CODEX_HOME"]).is_dir()
    if failure == "namespace":
        assert "namespace denied" in str(report)
