"""Live SV2-HDR-03 proof: saved machine registrations on a real herdr client against two worker sshds on localhost.

Usage: PYTHONPATH=<worktree> python tests/integration/herdr_registrations/registration_proof.py WORK_DIR
Reuses the SV2-HDR-02 worker harness; worker C listens on 127.0.0.2 so both endpoints share one client port.
"""

import importlib.util
import json
import shutil
import socket
import sys
import time
from pathlib import Path
from types import SimpleNamespace

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from scripts.swarm_v2.herdr.endpoints import Endpoint, HostIdentity, TerminalAuthority, operator_scope
from scripts.swarm_v2.herdr.reconcile import Report
from scripts.swarm_v2.operator_auth import OPERATOR

REPO = Path(__file__).resolve().parents[3]
_spec = importlib.util.spec_from_file_location(
    "terminal_proof", REPO / "tests/integration/herdr_terminal/terminal_proof.py"
)
tp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tp)

ENVIRONMENT = (
    "live on a workstation, not Anton: the pinned herdr client saves machines through herdr machine commands; "
    "two worker sshds from docker/swarm-node/ssh with supervised herdr servers run unprivileged on 127.0.0.1 and "
    "127.0.0.2; cluster DNS names are answered by an injected resolver"
)


def scope(exe: str):
    return operator_scope(OPERATOR, "fixture-swarm", SimpleNamespace(execution_id=exe, generation=1))


def listed(terminal) -> list[dict]:
    return json.loads(tp.run(terminal, ["herdr", "machine", "list", "--json"]).stdout)


def labels(terminal) -> list[str]:
    return sorted(machine["label"] for machine in listed(terminal))


def wait_port(address: str) -> bool:
    for _ in range(100):
        with socket.socket() as probe:
            if probe.connect_ex((address, tp.PORT)) == 0:
                return True
        time.sleep(0.1)
    return False


def main() -> int:
    authority = TerminalAuthority(Ed25519PrivateKey.generate())
    identity_a, identity_c = HostIdentity.generate(), HostIdentity.generate()
    worker_a = tp.Worker("a", tp.EXE_A, identity_a, authority.user_ca)
    worker_c = tp.Worker("c", tp.EXE_C, identity_c, authority.user_ca)
    worker_c.environ["SWARM_TERMINAL_LISTEN"] = "127.0.0.2"
    workers = [worker_a, worker_c]
    tp.record("environment", True, ENVIRONMENT)
    try:
        worker_a.start_sshd(identity_a)
        worker_c.start_sshd(identity_c)
        tp.record("workers_started", wait_port("127.0.0.2") and worker_a.start_herdr() and worker_c.start_herdr(), "")
        endpoint_a, endpoint_c = Endpoint(scope(tp.EXE_A), tp.NAMESPACE), Endpoint(scope(tp.EXE_C), tp.NAMESPACE)
        tp.served[endpoint_a.host], tp.served[endpoint_c.host] = "127.0.0.1", "127.0.0.2"
        operator = tp.client("client-operator", authority)
        operator.attach(scope(tp.EXE_A), identity_a.public)
        operator.register(scope(tp.EXE_C), identity_c.public)
        manual = tp.run(operator, ["herdr", "machine", "add", "--label", endpoint_c.name, endpoint_a.target])
        manual_id = next(m["id"] for m in listed(operator) if m["label"] == endpoint_c.name)
        tp.record("b_manual_machine_saved_by_hand", manual.returncode == 0, {"labels": labels(operator)})

        report = operator.registrations.reconcile([endpoint_a, endpoint_c], operator.qualifier)
        manual_after = [m for m in listed(operator) if m["id"] == manual_id]
        tp.record(
            "b_label_collision_not_adopted",
            report.refused == {tp.EXE_C: "label_collision"}
            and len(manual_after) == 1
            and manual_after[0]["target"] == endpoint_a.target
            and tp.EXE_C not in operator.registrations.store.load(),
            {"refused": report.refused, "drift": report.drift, "manual_kept": len(manual_after) == 1},
        )
        tp.run(operator, ["herdr", "machine", "remove", manual_id])

        report = operator.registrations.reconcile([endpoint_a, endpoint_c], operator.qualifier)
        records = operator.registrations.store.load()
        machines = {m["id"]: m for m in listed(operator)}
        mapped = {exe: machines[record.profile_id]["label"] for exe, record in records.items()}
        tp.record(
            "a_two_workers_beside_local",
            report.refused == {}
            and mapped == {tp.EXE_A: endpoint_a.name, tp.EXE_C: endpoint_c.name}
            and all(machines[record.profile_id]["session"] == "default" for record in records.values()),
            {"sidebar": ["Local", *labels(operator)], "registered": sorted(report.registered), "drift": report.drift},
        )
        status = [
            tp.run(operator, ["herdr", "--machine", endpoint.name, "status", "server", "--json"]).returncode
            for endpoint in (endpoint_a, endpoint_c)
        ]
        tp.record("a_each_registration_reaches_its_server", status == [0, 0], status)

        kept = records[tp.EXE_A].profile_id
        tp.run(operator, ["herdr", "machine", "remove", records[tp.EXE_C].profile_id])
        restarted = tp.client("client-operator", authority)
        report = restarted.registrations.reconcile([endpoint_a, endpoint_c], restarted.qualifier)
        after = restarted.registrations.store.load()
        tp.record(
            "c_restart_restores_missing_without_duplicates",
            report.drift == {"missing": 1}
            and after[tp.EXE_A].profile_id == kept
            and labels(restarted) == sorted([endpoint_a.name, endpoint_c.name]),
            {"drift": report.drift, "labels": labels(restarted)},
        )
        renamed = tp.run(restarted, ["herdr", "machine", "rename", kept, "--label", "by-hand"])
        tp.record(
            "c_hand_rename_applied",
            renamed.returncode == 0 and "by-hand" in labels(restarted),
            {
                "exit": renamed.returncode,
                "output": tp.scrub(f"{renamed.stdout}{renamed.stderr}"),
                "labels": labels(restarted),
            },
        )
        report = restarted.registrations.reconcile([endpoint_a, endpoint_c], restarted.qualifier)
        tp.record(
            "c_label_drift_renamed_back",
            report.drift == {"label": 1} and endpoint_a.name in labels(restarted),
            {"drift": report.drift, "labels": labels(restarted)},
        )
        replay = restarted.registrations.reconcile([endpoint_a, endpoint_c], restarted.qualifier)
        tp.record("c_replay_has_no_drift", replay.herdr_registration_drift() == 0, replay.drift)

        report = restarted.registrations.reconcile([endpoint_a], restarted.qualifier)
        status_c = tp.subprocess.run(
            [str(tp.HERDR), "status", "server", "--json"], env=worker_c.herdr_env(), capture_output=True, text=True
        )
        running = '"running":true' in status_c.stdout
        tp.record(
            "c_retired_registration_removed_and_its_server_keeps_running",
            report.retired == [tp.EXE_C] and labels(restarted) == [endpoint_a.name] and running,
            {"retired": report.retired, "labels": labels(restarted), "worker_c_server_running": running},
        )
        both = sorted([endpoint_a.name, endpoint_c.name])
        synced = restarted.sync(
            [(scope(tp.EXE_A), identity_a.public), (scope(tp.EXE_C), identity_c.public)], enabled=True
        )
        tp.record(
            "a_sync_registers_every_live_attempt",
            synced.refused == {} and sorted(synced.registered) == [tp.EXE_A, tp.EXE_C] and labels(restarted) == both,
            {"registered": sorted(synced.registered), "drift": synced.drift, "labels": labels(restarted)},
        )
        stopped = restarted.sync([], enabled=False)
        tp.record(
            "rollback_stopped_registration_leaves_profiles",
            stopped == Report() and labels(restarted) == both,
            {"labels": labels(restarted)},
        )
    finally:
        for worker in workers:
            worker.stop_sshd()
            worker.stop_herdr()
        for key in tp.root.glob("client-*/.ssh/swarm-terminals/id_ed25519"):
            key.unlink()
        shutil.rmtree(tp.WORKERS)
        (tp.root / "results.jsonl").write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in tp.rows))
    print(f"failures={tp.failures}", flush=True)
    return 1 if tp.failures else 0


if __name__ == "__main__":
    sys.exit(main())
