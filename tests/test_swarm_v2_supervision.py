import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from scripts.swarm_v2.supervision import Launch, LaunchRefused
from scripts.swarm_v2.supervision_agent import native_command
from scripts.swarm_v2.supervision_protocol import checkpoint, write
from scripts.swarm_v2.supervision_runtime import Supervisor, main


def launch_files(tmp_path):
    attempt = tmp_path / "attempt"
    attempt.mkdir()
    (attempt / "homes" / "codex").mkdir(parents=True)
    authority = {
        "execution_id": "exe-" + "a" * 32,
        "generation": 1,
        "task_id": "fixture",
        "seat_id": "eng-1",
        "swarm_id": "fixture",
        "grant_id": "lgr-" + "b" * 32,
    }
    record = {"attempt": authority["execution_id"], "homes": {"codex": "homes/codex"}}
    (attempt / "execution.json").write_text(json.dumps(record))
    (attempt / "registration.json").write_text(json.dumps(authority))
    spec = {"schema_version": 1, "authority": authority, "harness": "codex", "agent": ["true"], "exporter": ["true"]}
    path = tmp_path / "launch.json"
    path.write_text(json.dumps(spec))
    return attempt, path, spec


def test_launch_binds_registered_authority_and_private_home(tmp_path):
    attempt, path, spec = launch_files(tmp_path)
    launch = Launch.load(attempt, path)
    assert launch.authority == spec["authority"]
    assert launch.home == attempt / "homes/codex"
    assert launch.herdr == ("herdr", "server")


@pytest.mark.parametrize("field,value", [("generation", 2), ("execution_id", "exe-" + "c" * 32), ("grant_id", "other")])
def test_launch_refuses_authority_changes_before_creating_runtime(tmp_path, field, value):
    attempt, path, spec = launch_files(tmp_path)
    spec["authority"][field] = value
    path.write_text(json.dumps(spec))
    with pytest.raises(LaunchRefused, match="authority"):
        Launch.load(attempt, path)
    assert not (attempt / "run").exists()


@pytest.mark.parametrize("value", [0, -1, True, float("inf")])
def test_launch_refuses_unbounded_deadline(tmp_path, value):
    attempt, path, spec = launch_files(tmp_path)
    spec["quiesce_seconds"] = value
    path.write_text(json.dumps(spec))
    with pytest.raises(LaunchRefused, match="deadline"):
        Launch.load(attempt, path)


def test_launch_refuses_home_escape(tmp_path):
    attempt, path, _ = launch_files(tmp_path)
    (attempt / "execution.json").write_text(
        json.dumps({"attempt": "exe-" + "a" * 32, "homes": {"codex": str(tmp_path)}})
    )
    with pytest.raises(LaunchRefused, match="home"):
        Launch.load(attempt, path)


def wait_for(path, child):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        if path.exists():
            return json.loads(path.read_text())
        if child.poll() is not None:
            raise AssertionError(f"supervisor exited before {path.name}: {child.returncode}")
        time.sleep(0.02)
    raise AssertionError(f"no {path.name} receipt")


@pytest.fixture
def worker(tmp_path):
    attempt, path, spec = launch_files(tmp_path)
    fixture = Path(__file__).parent / "integration/swarm_node/process_fixture.py"
    spec.update(
        herdr=[sys.executable, str(fixture), "herdr", "server"],
        agent=[sys.executable, str(fixture), "agent", "cooperative"],
        exporter=[sys.executable, str(fixture), "exporter", "0.1", "complete"],
        startup_seconds=3,
        quiesce_seconds=0.2,
        checkpoint_seconds=0.4,
        kill_seconds=0.2,
    )
    children = []

    def start(**changes):
        spec.update(changes)
        path.write_text(json.dumps(spec))
        environment = {k: v for k, v in os.environ.items() if "REDIS" not in k}
        environment["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent)
        child = subprocess.Popen(
            [sys.executable, "-m", "scripts.swarm_v2.supervision_runtime", str(attempt), str(path)],
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        children.append(child)
        return child

    yield start, attempt, spec
    for child in children:
        if child.poll() is None:
            child.terminate()
        child.wait(timeout=8)


def runtime_directory(attempt, child):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        directories = sorted((attempt / "run/supervision").glob("*/context.json"))
        if directories:
            return directories[-1].parent
        if child.poll() is not None:
            raise AssertionError(f"supervisor launch failed: {child.returncode}")
        time.sleep(0.02)
    raise AssertionError("supervisor did not create a launch context")


@pytest.mark.parametrize("ack,expected", [("complete", "complete"), ("stale", "incomplete"), ("missing", "incomplete")])
def test_sigterm_during_tool_reports_observed_checkpoint(worker, ack, expected):
    start, attempt, spec = worker
    exporter = [*spec["exporter"][:-1], ack]
    child = start(exporter=exporter)
    root = runtime_directory(attempt, child)
    wait_for(root / "running.json", child)
    wait_for(root / "fixture-grandchild-two.json", child)
    child.send_signal(signal.SIGTERM)
    result = wait_for(root / "result.json", child)
    assert child.wait(timeout=8) == (0 if expected == "complete" else 75)
    assert result["reason"] == "termination"
    assert result["checkpoint_status"] == expected
    assert result["quiescence"] == "clean"
    assert result["supervisor_child_exit_total"] >= 2


def test_exporter_readiness_precedes_agent_launch(worker):
    start, attempt, spec = worker
    child = start(exporter=[sys.executable, "-c", "import time; time.sleep(20)"], startup_seconds=1)
    root = runtime_directory(attempt, child)
    result = wait_for(root / "result.json", child)
    assert child.wait(timeout=8) == 70
    assert result["reason"] == "exporter_startup_failure"
    assert not (root / "fixture-agent.json").exists()


def test_detaching_viewers_keeps_agent_exporter_and_grandchildren_alive(worker):
    start, attempt, _ = worker
    child = start()
    root = runtime_directory(attempt, child)
    wait_for(root / "running.json", child)
    roles = ("agent", "tool", "grandchild-one", "grandchild-two", "exporter")
    before = {role: wait_for(root / f"heartbeat-{role}.json", child)["tick"] for role in roles}
    viewer = subprocess.Popen([sys.executable, "-c", "pass"])
    assert viewer.wait() == 0
    time.sleep(0.1)
    after = {role: json.loads((root / f"heartbeat-{role}.json").read_text())["tick"] for role in roles}
    assert all(after[role] > before[role] for role in roles)
    assert child.poll() is None


@pytest.mark.parametrize("mode,reason,code", [("complete", "agent_completed", 0), ("fail", "agent_failure", 70)])
def test_agent_completion_is_distinct_from_failure(worker, mode, reason, code):
    start, attempt, spec = worker
    child = start(agent=[*spec["agent"][:-1], mode])
    root = runtime_directory(attempt, child)
    result = wait_for(root / "result.json", child)
    assert child.wait(timeout=8) == code
    assert result["reason"] == reason


def test_stubborn_grandchildren_force_incomplete_checkpoint(worker):
    start, attempt, spec = worker
    child = start(agent=[*spec["agent"][:-1], "stubborn"])
    root = runtime_directory(attempt, child)
    wait_for(root / "fixture-grandchild-two.json", child)
    child.terminate()
    result = wait_for(root / "result.json", child)
    assert child.wait(timeout=8) == 75
    assert result["quiescence"] == "forced"
    assert result["checkpoint_status"] == "incomplete"


def test_main_agent_abrupt_death_is_observed_without_exit_receipt(worker):
    start, attempt, _ = worker
    child = start()
    root = runtime_directory(attempt, child)
    ready = wait_for(root / "agent.json", child)
    os.kill(ready["pid"], signal.SIGKILL)
    result = wait_for(root / "result.json", child)
    assert child.wait(timeout=8) == 70
    assert result["reason"] == "agent_failure"


def test_only_one_supervisor_owns_an_attempt(worker):
    start, attempt, _ = worker
    first = start()
    root = runtime_directory(attempt, first)
    wait_for(root / "running.json", first)
    second = start()
    assert second.wait(timeout=8) == 64
    assert first.poll() is None
    assert len(list((attempt / "run/supervision").glob("*/context.json"))) == 1


def test_private_native_homes_override_inherited_workstation_settings(tmp_path, monkeypatch):
    attempt, path, _ = launch_files(tmp_path)
    monkeypatch.setenv("CODEX_HOME", "/workstation/codex")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/workstation/claude")
    monkeypatch.setenv("HERDR_SESSION", "workstation")
    worker = Supervisor(Launch.load(attempt, path))
    assert worker.environment["CODEX_HOME"] == str(attempt / "homes/codex/.codex")
    assert worker.environment["CLAUDE_CONFIG_DIR"] == str(attempt / "homes/codex/.claude")
    assert "HERDR_SESSION" not in worker.environment
    assert worker.environment["HERDR_CONFIG_PATH"].startswith(str(attempt))


def test_native_codex_trust_is_scoped_to_admitted_directory(tmp_path):
    result = native_command(("codex", "exec", "fixture"), tmp_path, {})
    assert result == (
        "codex",
        "-c",
        f'projects={{{json.dumps(str(tmp_path))}={{trust_level="trusted"}}}}',
        "exec",
        "fixture",
    )


@pytest.mark.parametrize("fault", ["hash", "scope", "status", "absolute", "traversal", "missing", "not-object"])
def test_checkpoint_requires_matching_durable_manifest(tmp_path, fault):
    attempt = tmp_path
    scope = {"authority": {"execution_id": "fixture", "generation": 1}, "incarnation": "current"}
    manifest = attempt / "checkpoints/current/manifest.json"
    manifest.parent.mkdir(parents=True)
    write(manifest, {**scope, "status": "complete", "checkpoint_id": "checkpoint"})
    receipt = {
        **scope,
        "status": "complete",
        "manifest": "checkpoints/current/manifest.json",
        "sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
    }
    assert checkpoint(attempt, receipt, scope) == "checkpoint"
    if fault == "hash":
        receipt["sha256"] = "unverified"
    elif fault == "scope":
        receipt["incarnation"] = "old"
    elif fault == "status":
        receipt["status"] = "uploading"
    elif fault == "absolute":
        receipt["manifest"] = str(manifest)
    elif fault == "traversal":
        receipt["manifest"] = "checkpoints/../checkpoints/current/manifest.json"
    elif fault == "missing":
        receipt["manifest"] = "checkpoints/absent.json"
    else:
        manifest.write_text("[]")
        receipt["sha256"] = hashlib.sha256(manifest.read_bytes()).hexdigest()
    assert checkpoint(attempt, receipt, scope) is None


def test_launch_refusal_never_prints_user_input(capsys):
    assert main(["fixture"]) == 64
    assert "requires attempt directory" in capsys.readouterr().err


def test_checkpoint_uses_the_exact_acknowledged_bytes(tmp_path, monkeypatch):
    scope = {"authority": {"execution_id": "fixture"}, "incarnation": "current"}
    path = tmp_path / "checkpoints/current/manifest.json"
    path.parent.mkdir(parents=True)
    write(path, {**scope, "status": "complete", "checkpoint_id": "acknowledged"})
    raw = path.read_bytes()
    receipt = {
        **scope,
        "status": "complete",
        "manifest": "checkpoints/current/manifest.json",
        "sha256": hashlib.sha256(raw).hexdigest(),
    }
    original = Path.read_bytes

    def replace_after_read(file):
        result = original(file)
        if file == path:
            write(path, {**scope, "status": "complete", "checkpoint_id": "unacknowledged"})
        return result

    monkeypatch.setattr(Path, "read_bytes", replace_after_read)
    assert checkpoint(tmp_path, receipt, scope) == "acknowledged"
