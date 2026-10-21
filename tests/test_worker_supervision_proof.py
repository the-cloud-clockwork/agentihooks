import itertools
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
        ticks=iter((1, 2)),
        beats=5,
        now=100,
        clock_step=0,
    )

    def docker(*args, timeout=30):
        observed.calls.append((args, timeout))
        if args[0] == "run":
            return "container"
        if args[0] == "exec":
            if "supervisor_pid" in args[-1]:
                return "1"
            tick = next(observed.ticks)
            if isinstance(tick, Exception):
                raise tick
            return json.dumps(
                tick if isinstance(tick, dict) else {f"heartbeat-{i}.json": tick for i in range(observed.beats)}
            )
        if args[0] == "wait":
            observed.now += observed.elapsed
            if observed.wait_error:
                raise observed.wait_error
            return str(observed.code)
        if args[0] == "cp":
            destination = Path(args[2])
            if args[1].endswith("launch.json"):
                destination.write_text(json.dumps({"quiesce_seconds": 2, "checkpoint_seconds": 2, "kill_seconds": 1}))
            elif observed.results:
                root = destination / "supervision" / "incarnation"
                root.mkdir(parents=True)
                (root / "result.json").write_text(json.dumps({"checkpoint_status": observed.checkpoint}))
        return ""

    monkeypatch.setattr(proof, "docker", docker)
    monkeypatch.setattr(proof, "ready", lambda _: {"root": "/home/worker/attempts/fixture/run/supervision/incarnation"})
    monkeypatch.setattr(proof, "state", lambda _: {"Running": observed.running, "Pid": observed.pid})
    monkeypatch.setattr(proof.time, "sleep", lambda _: None)

    def monotonic():
        observed.now += observed.clock_step
        return observed.now

    monkeypatch.setattr(proof.time, "monotonic", monotonic)
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
    assert result["supervision_budget_seconds"] == 8
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


@pytest.mark.parametrize("error", [subprocess.CalledProcessError(1, "docker"), ValueError("bad json")])
def test_a_failed_heartbeat_read_is_retried(container, tmp_path, error):
    container.ticks = iter((error, 1, 2))

    result = proof.run_case("image", "complete", tmp_path, viewer=False)

    assert result["exit_code"] == 0
    assert container.calls[-1][0] == ("rm", "--force", "container")


@pytest.mark.parametrize("beats", [5, 4])
def test_heartbeats_never_accepted_fail_and_the_container_is_removed(container, tmp_path, beats):
    container.ticks = itertools.repeat(1)
    container.beats = beats
    container.clock_step = 1

    with pytest.raises(AssertionError, match="heartbeats not accepted"):
        proof.run_case("image", "complete", tmp_path, viewer=False)

    assert container.calls[-1][0] == ("rm", "--force", "container")


@pytest.mark.parametrize(
    "attribute,value", [("code", 75), ("checkpoint", "incomplete"), ("running", True), ("pid", 42), ("results", False)]
)
def test_incorrect_supervision_outcomes_still_fail(container, tmp_path, attribute, value):
    setattr(container, attribute, value)

    with pytest.raises(AssertionError):
        proof.run_case("image", "complete", tmp_path, viewer=False)

    assert container.calls[-1][0] == ("rm", "--force", "container")


def test_delayed_heartbeats_are_observed_before_termination(container, tmp_path):
    initial = {f"heartbeat-{i}.json": 1 for i in range(5)}
    partial = {key: 2 if key != "heartbeat-4.json" else 1 for key in initial}
    advanced = {key: 2 for key in initial}
    container.ticks = iter(({}, dict(list(initial.items())[:4]), initial, initial, partial, advanced))

    proof.run_case("image", "complete", tmp_path, viewer=False)

    samples = [args for args, _ in container.calls if args[0] == "exec"]
    assert len(samples) == 6
    assert (("kill", "--signal", "TERM", "container"), 30) in container.calls


@pytest.mark.parametrize("missing", [False, True])
def test_stalled_or_missing_heartbeat_fails_before_termination(container, tmp_path, missing):
    from itertools import chain, repeat

    initial = {f"heartbeat-{i}.json": 1 for i in range(5)}
    stalled = dict(initial)
    if missing:
        del stalled["heartbeat-4.json"]
    container.ticks = chain((initial,), repeat(stalled))
    container.clock_step = 1

    with pytest.raises(AssertionError):
        proof.run_case("image", "complete", tmp_path, viewer=False)

    assert not any(args[0] == "kill" for args, _ in container.calls)
    assert container.calls[-1][0] == ("rm", "--force", "container")
