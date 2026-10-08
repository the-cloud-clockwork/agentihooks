import hashlib
import json
import subprocess
import sys
import tempfile
from dataclasses import asdict
from pathlib import Path

import pytest

from scripts.swarm.pane import PaneObservation
from scripts.swarm.store import AgentRecord
from scripts.swarm_v2.runtime.base import LOCAL, Recovery, RuntimeRouter, SpawnRequest, Status
from scripts.swarm_v2.runtime.local import LocalHerdrRuntime
from tests.test_swarm_v2_runtime import PRIVATE, REMOTE, config, herdr_runtime, local_fake, remote_fake, request

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / "evidence/SV2-RUN-01"
NAME = "engineer@a1b2c3-0001"
EVIDENCE_CLASS = (
    "local isolated fixture on temporary homes; mocked: the herdr CLI, the init-agent launch and the process reaper "
    "behind HerdrRuntime, and the remote backend is an in-memory fake; not live rollout proof"
)
INPUTS = (
    "scripts/swarm_v2/runtime/base.py",
    "scripts/swarm_v2/runtime/local.py",
    "scripts/swarm/runtime.py",
    "scripts/swarm/store.py",
    "tests/test_swarm_v2_runtime.py",
    "evidence/SV2-RUN-01/generate_case_results.py",
)


def local_swarm(root: Path) -> dict:
    with pytest.MonkeyPatch.context() as monkeypatch:
        pane = {"pane_id": "w1:p1", "name": NAME, "agent_status": "working"}
        herdr, calls = herdr_runtime(root, monkeypatch, panes={"w1:p1": pane})
        remote = remote_fake()
        router = RuntimeRouter([LocalHerdrRuntime(herdr), remote])
        spawned = router.spawn(
            SpawnRequest(config(root), "eng", NAME, {"id": "t1", "title": "x", "profile": "engineer"})
        )
        agent = AgentRecord(NAME, "eng", "t1", pane_id=spawned.value.pane_id)
        observed = router.observe(agent)
        retired = router.terminate(agent)
    return {
        "spawn": [spawned.status, spawned.backend, spawned.value.pane_id],
        "observe": [observed.status, observed.value.state],
        "terminate": [retired.status, retired.backend],
        "pane_closed": ["pane", "close", "w1:p1"] in calls,
        "remote_calls": len(remote.calls),
        "runtime_capability_failures_total": router.capability_failures_total(),
    }


def case_a() -> dict:
    runs = []
    for _ in range(2):
        with tempfile.TemporaryDirectory() as root:
            runs.append(local_swarm(Path(root)))
    local, remote = local_fake(), remote_fake()
    router = RuntimeRouter([local, remote], REMOTE)
    placed = router.spawn(request()).value
    routed = {
        "spawned_on": placed.runtime_backend,
        "observe": router.observe(placed).status,
        "drain": router.drain(placed).status,
        "terminate": router.terminate(placed).status,
        "local_calls": len(local.calls),
        "remote_object_removed": placed.name not in remote.objects,
        "runtime_capability_failures_total": router.capability_failures_total(),
    }
    passed = all(
        run["spawn"] == [Status.OK, LOCAL, "w1:p1"]
        and run["observe"] == [Status.OK, "working"]
        and run["terminate"] == [Status.OK, LOCAL]
        and run["pane_closed"]
        and run["remote_calls"] == 0
        and run["runtime_capability_failures_total"] == 0
        for run in runs
    ) and routed == {
        "spawned_on": REMOTE,
        "observe": Status.OK,
        "drain": Status.OK,
        "terminate": Status.OK,
        "local_calls": 0,
        "remote_object_removed": True,
        "runtime_capability_failures_total": 0,
    }
    return {
        "then": "the existing local swarm completes its original spawn, observe and retire tests through the new interface",
        "runs": runs,
        "remote_routing": routed,
        "passed": passed,
    }


def case_b() -> dict:
    local, remote = local_fake(), remote_fake()
    router = RuntimeRouter([local, remote], REMOTE)
    agent = router.spawn(request()).value
    before = {name: asdict(row) for name, row in remote.objects.items()}
    outcome = router.recover(agent, Recovery.RESUME, text="you were restored")
    result = {
        "status": outcome.status,
        "detail": outcome.detail,
        "remote_state_unchanged": {name: asdict(row) for name, row in remote.objects.items()} == before,
        "remote_calls_after_spawn": len(remote.calls) - 1,
        "local_calls": len(local.calls),
        "discloses_runtime_target": PRIVATE in repr(outcome),
        "runtime_capability_failures_total": {f"{b}/{op}": n for (b, op), n in router.failures.items()},
    }
    passed = result == {
        "status": Status.UNSUPPORTED,
        "detail": "kubernetes lacks native_resume",
        "remote_state_unchanged": True,
        "remote_calls_after_spawn": 0,
        "local_calls": 0,
        "discloses_runtime_target": False,
        "runtime_capability_failures_total": {f"{REMOTE}/recover": 1},
    }
    return {
        "then": "a backend lacking native resume reports unsupported capability rather than silently starting fresh",
        "result": result,
        "passed": passed,
    }


def case_c() -> dict:
    local, remote = local_fake(), remote_fake()
    attempt = RuntimeRouter([local, remote], REMOTE).spawn(request(task="t1")).value
    history = asdict(attempt)
    rolled = RuntimeRouter([local, remote], REMOTE, disabled=[REMOTE])
    fresh = rolled.spawn(request("engineer@a1b2c3-0002", "t2")).value
    stopped = rolled.terminate(attempt)
    restored = RuntimeRouter([local, remote], REMOTE)
    observed = restored.observe(attempt)
    replayed = restored.spawn(request(task="t1")).value
    result = {
        "new_spawn_backend_while_disabled": fresh.runtime_backend,
        "disabled_backend_operation": [stopped.status, stopped.detail],
        "attempt_record_unchanged": asdict(attempt) == history,
        "remote_object_kept": attempt.name in remote.objects,
        "observe_after_reenable": [observed.status, observed.value == PaneObservation("working")],
        "replay_returns_first_object": replayed is remote.objects[attempt.name],
        "remote_objects": len(remote.objects),
        "runtime_capability_failures_total": rolled.capability_failures_total() + restored.capability_failures_total(),
    }
    passed = result == {
        "new_spawn_backend_while_disabled": LOCAL,
        "disabled_backend_operation": [Status.UNAVAILABLE, "runtime kubernetes is disabled"],
        "attempt_record_unchanged": True,
        "remote_object_kept": True,
        "observe_after_reenable": [Status.OK, True],
        "replay_returns_first_object": True,
        "remote_objects": 1,
        "runtime_capability_failures_total": 0,
    }
    return {
        "then": "an adapter upgrade can be disabled without rewriting existing task records",
        "result": result,
        "passed": passed,
    }


def main() -> int:
    if subprocess.run(["git", "diff", "--quiet", "HEAD", "--", *INPUTS], cwd=ROOT).returncode:
        print("commit the case inputs first: results must name the commit that holds them")
        return 2
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.strip()
    outcomes = {}
    for name, case in (("a", case_a), ("b", case_b), ("c", case_c)):
        result = {"tested_commit": commit, "evidence_class": EVIDENCE_CLASS, **case()}
        outcomes[name] = result["passed"]
        (OUTPUT / f"{name}-result.json").write_text(json.dumps(result, indent=2) + "\n")
    manifest = {
        "tested_commit": commit,
        "evidence_class": EVIDENCE_CLASS,
        "inputs": {path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest() for path in INPUTS},
    }
    (OUTPUT / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps(outcomes))
    return 0 if all(outcomes.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
