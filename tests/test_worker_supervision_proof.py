import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.integration.swarm_node import supervision_proof as proof


@pytest.fixture
def container(monkeypatch):
    observed = SimpleNamespace(
        elapsed=0.5,
        code=0,
        checkpoint="complete",
        running=False,
        pid=0,
        wait_error=None,
        results=True,
        calls=[],
    )
    ticks = iter((1, 2))
    clock = iter((False, True))

    def docker(*args, timeout=30):
        observed.calls.append((args, timeout))
        if args[0] == "run":
            return "container"
        if args[0] == "exec":
            if "supervisor_pid" in args[-1]:
                return "1"
            tick = next(ticks)
            return json.dumps({f"heartbeat-{i}.json": tick for i in range(5)})
        if args[0] == "wait":
            if observed.wait_error:
                raise observed.wait_error
            return str(observed.code)
        if args[0] == "cp":
            destination = Path(args[2])
            if args[1].endswith("launch.json"):
                destination.write_text(json.dumps({"quiesce_seconds": 2, "checkpoint_seconds": 0.8, "kill_seconds": 1}))
            elif observed.results:
                root = destination / "supervision" / "incarnation"
                root.mkdir(parents=True)
                (root / "result.json").write_text(json.dumps({"checkpoint_status": observed.checkpoint}))
        return ""

    monkeypatch.setattr(proof, "docker", docker)
    monkeypatch.setattr(proof, "ready", lambda _: {"root": "/home/worker/attempts/fixture/run/supervision/incarnation"})
    monkeypatch.setattr(proof, "state", lambda _: {"Running": observed.running, "Pid": observed.pid})
    monkeypatch.setattr(proof.time, "sleep", lambda _: None)
    monkeypatch.setattr(proof.time, "monotonic", lambda: 100 + (observed.elapsed if next(clock) else 0))
    return observed


@pytest.mark.parametrize(
    "mode,kill,supervised,code",
    [("complete", False, True, 0), ("forced", False, True, 75), ("complete", True, False, 137)],
)
def test_delayed_host_observation_accepts_the_container_exit_event(container, tmp_path, mode, kill, supervised, code):
    container.elapsed = 20
    container.code = code
    container.results = not kill
    container.checkpoint = "complete" if mode == "complete" else "incomplete"

    result = proof.run_case("image", mode, tmp_path, kill=kill, supervised=supervised, viewer=False)

    assert result["elapsed_shutdown_seconds"] == 20
    assert result["supervision_budget_seconds"] == 6.8
    assert result["shutdown_wait_timeout_seconds"] == 12
    assert result["container_process_tree_gone"] is True
    assert (("wait", "container"), 12) in container.calls
    assert container.calls[-1][0] == ("rm", "--force", "container")


def test_a_container_that_never_exits_still_fails_and_is_removed(container, tmp_path):
    container.wait_error = subprocess.TimeoutExpired(["docker", "wait", "container"], 12)

    with pytest.raises(subprocess.TimeoutExpired):
        proof.run_case("image", "complete", tmp_path, viewer=False)

    assert (("wait", "container"), 12) in container.calls
    assert container.calls[-1][0] == ("rm", "--force", "container")


@pytest.mark.parametrize(
    "attribute,value", [("code", 75), ("checkpoint", "incomplete"), ("running", True), ("pid", 42), ("results", False)]
)
def test_incorrect_supervision_outcomes_still_fail(container, tmp_path, attribute, value):
    setattr(container, attribute, value)

    with pytest.raises(AssertionError):
        proof.run_case("image", "complete", tmp_path, viewer=False)

    assert container.calls[-1][0] == ("rm", "--force", "container")
