import hashlib
import json
import subprocess
import sys
import tempfile
from dataclasses import asdict, replace
from pathlib import Path

import fakeredis
import pytest

from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig
from scripts.swarm.tick import tick
from scripts.swarm_v2.runtime.base import LOCAL, RuntimeRouter, Status
from tests.swarm.test_tick import FakeLedger
from tests.swarm.test_tick import FakeRuntime as TickRuntime
from tests.test_swarm_v2_process import ANTON, NAME, PID, WORKER, adapter, proc, record
from tests.test_swarm_v2_runtime import REMOTE, remote_fake, request

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / "evidence/SV2-RUN-02"
EVIDENCE_CLASS = (
    "local isolated fixture on temporary homes; mocked: the herdr CLI and the process reaper behind HerdrRuntime, "
    "the local process table and PID namespace are fixture values, and the remote backend is an in-memory fake; "
    "not live rollout proof"
)
INPUTS = (
    "scripts/swarm_v2/runtime/base.py",
    "scripts/swarm_v2/runtime/local.py",
    "scripts/swarm_v2/runtime/process.py",
    "scripts/swarm_v2/runtime/routed.py",
    "scripts/swarm/runtime.py",
    "scripts/swarm/tick.py",
    "scripts/swarm/execution.py",
    "scripts/swarm/reaper.py",
    "tests/test_swarm_v2_process.py",
    "tests/test_swarm_v2_runtime.py",
    "tests/swarm/test_tick.py",
    "evidence/SV2-RUN-02/generate_case_results.py",
)


def colliding(root: Path) -> dict:
    with pytest.MonkeyPatch.context() as monkeypatch:
        local, calls, ended = adapter(root, monkeypatch, {PID: proc()})
        remote = remote_fake()
        router = RuntimeRouter([local, remote], REMOTE)
        on_worker = router.spawn(request("engineer@a1b2c3-0002", "t2")).value
        on_anton = record()
        impostor = replace(record(namespace=WORKER), name="engineer@a1b2c3-0003")
        worker_end = router.terminate(on_worker)
        anton_end = router.terminate(on_anton)
        impostor_end = router.terminate(impostor)
    return {
        "worker_terminate": [worker_end.status, worker_end.backend],
        "anton_terminate": [anton_end.status, anton_end.backend],
        "local_signalled": [[name, pid] for name, pid, _ in ended],
        "remote_terminated": [name for op, name in remote.calls if op == "terminate"],
        "foreign_namespace_local_record": [impostor_end.status, impostor_end.detail],
        "unqualified_process_actions_rejected_total": router.unqualified_process_actions_rejected_total(),
    }


def case_a() -> dict:
    runs = []
    for _ in range(2):
        with tempfile.TemporaryDirectory() as root:
            runs.append(colliding(Path(root)))
    expected = {
        "worker_terminate": [Status.OK, REMOTE],
        "anton_terminate": [Status.OK, LOCAL],
        "local_signalled": [[NAME, PID]],
        "remote_terminated": ["engineer@a1b2c3-0002"],
        "foreign_namespace_local_record": [Status.REFUSED, "process belongs to another PID namespace"],
        "unqualified_process_actions_rejected_total": 1,
    }
    return {
        "then": "an AWS worker and Anton may both have PID 4321 without either being mistaken for the other's agent",
        "fixture": {"pid": PID, "anton_namespace": ANTON, "worker_namespace": WORKER},
        "runs": runs,
        "passed": all(run == expected for run in runs),
    }


def case_b() -> dict:
    with tempfile.TemporaryDirectory() as root, pytest.MonkeyPatch.context() as monkeypatch:
        local, calls, ended = adapter(Path(root), monkeypatch, {PID: proc()})
        remote = remote_fake()
        router = RuntimeRouter([local, remote])
        bare = record(execution="")
        before = asdict(bare)
        refused = router.terminate(bare)
        remote_bare = AgentRecord("engineer@a1b2c3-0002", "eng", "t2", runtime_backend=REMOTE)
        remote_refused = router.terminate(remote_bare)
        refusals = router.unqualified_process_actions_rejected_total()
        signals, pane_commands = len(ended), len(calls)
        corrected = router.terminate(record())
        result = {
            "status": refused.status,
            "detail": refused.detail,
            "signals_sent": signals,
            "pane_commands_before_correction": pane_commands,
            "record_unchanged": asdict(bare) == before,
            "discloses_runtime_target": ANTON in repr(refused) or str(PID) in refused.detail,
            "remote_without_identity": [remote_refused.status, remote_refused.detail, len(remote.calls)],
            "unqualified_process_actions_rejected_total": refusals,
            "rejections_by_backend": sorted([backend, reason] for (backend, reason) in router.rejected),
            "corrected_request": [corrected.status, [[name, pid] for name, pid, _ in ended]],
        }
    passed = result == {
        "status": Status.REFUSED,
        "detail": "no execution identity",
        "signals_sent": 0,
        "pane_commands_before_correction": 0,
        "record_unchanged": True,
        "discloses_runtime_target": False,
        "remote_without_identity": [Status.REFUSED, "no execution identity", 0],
        "unqualified_process_actions_rejected_total": 2,
        "rejections_by_backend": [[REMOTE, "no execution identity"], [LOCAL, "no execution identity"]],
        "corrected_request": [Status.OK, [[NAME, PID]]],
    }
    return {
        "then": "a terminate request lacking execution identity fails even when a local PID happens to match",
        "result": result,
        "passed": passed,
    }


def lost_remote() -> dict:
    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("sw", "/repo", max_eng=1, max_ci=0))
    name = store.next_name("sw", "eng")
    target = {"pod_namespace": "workers", "pod_name": "worker-1"}
    agent = AgentRecord(
        name, "eng", "t1", seat="eng-1@sw", started_at=1_000, runtime_backend=REMOTE, runtime_target=target
    )
    agent = store.start_execution("sw", agent)
    store.claim("sw", "t1", name, 60_000)
    ledger = FakeLedger([{"id": "t1", "lane": "eng", "state": "claimed", "claimed_by": name}])
    runtime = TickRuntime()
    runtime.live.add(f"local-holder-of-pid-{PID}")
    runtime.statuses[name] = "unknown"
    late = 1_000 + 10 * 60_000
    first = tick("sw", store, ledger, runtime, now_ms=late)
    second = tick("sw", store, ledger, runtime, now_ms=late + 1)
    suspect = {a.name: a for a in store.agents("sw")}[name]
    runtime.statuses[name] = "working"
    tick("sw", store, ledger, runtime, now_ms=late + 2)
    restored = {a.name: a for a in store.agents("sw")}[name]
    return {
        "suspect_after_lost_observation": suspect.state,
        "suspect_actions": [line.replace(name, "<agent>") for line in [*first, *second] if "suspect" in line],
        "execution_identity_kept": [
            suspect.execution_id == agent.execution_id,
            restored.execution_id == agent.execution_id,
        ],
        "local_kills": runtime.killed,
        "local_retires": sorted(runtime.homes),
        "local_reaps": runtime.reaped,
        "task_claim_kept": ledger.rows["t1"]["claimed_by"] == name,
        "replacement_spawns": len(runtime.spawned),
        "state_after_observation_restored": restored.state,
    }


def case_c() -> dict:
    runs = [lost_remote() for _ in range(2)]
    expected = {
        "suspect_after_lost_observation": "suspect",
        "suspect_actions": ["suspect <agent>: its runtime did not answer"],
        "execution_identity_kept": [True, True],
        "local_kills": [],
        "local_retires": [],
        "local_reaps": [],
        "task_claim_kept": True,
        "replacement_spawns": 0,
        "state_after_observation_restored": "working",
    }
    return {
        "then": "lost remote observation produces suspect status and cannot cause an unrelated local process kill",
        "runs": runs,
        "passed": all(run == expected for run in runs),
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
