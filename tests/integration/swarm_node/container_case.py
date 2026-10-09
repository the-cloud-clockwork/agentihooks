import sys
import uuid
from pathlib import Path

from scripts.swarm_v2.supervision import Launch
from scripts.swarm_v2.supervision_protocol import write
from scripts.swarm_v2.supervision_runtime import Supervisor
from scripts.swarm_v2.worker_home import Request, bootstrap


def main():
    mode = sys.argv[1]
    identifier = "exe-" + uuid.uuid4().hex
    base = Path("/home/worker/attempts")
    base.mkdir(mode=0o700)
    request = Request(
        attempt=identifier,
        profiles={"codex": "fixture-codex"},
        root=base,
        templates=Path("/opt/fixture/profiles"),
        interpreter=Path("/opt/venv/bin/python"),
        accounts={},
        endpoints={},
        uid=10001,
        gid=10001,
    )
    bootstrap(request)
    attempt = base / identifier
    authority = {
        "execution_id": identifier,
        "generation": 1,
        "task_id": "fixture",
        "seat_id": "eng-1",
        "swarm_id": "fixture",
        "grant_id": "lgr-" + uuid.uuid4().hex,
    }
    write(attempt / "registration.json", authority)
    fixture = "/opt/fixture/process_fixture.py"
    delay = "2" if mode == "late" else "0.2"
    agent_mode = "stubborn" if mode == "forced" else "complete" if mode == "natural" else "cooperative"
    spec = {
        "schema_version": 1,
        "authority": authority,
        "harness": "codex",
        "agent": [sys.executable, fixture, "agent", agent_mode],
        "exporter": [sys.executable, fixture, "exporter", delay, "missing" if mode == "missing" else "complete"],
        "startup_seconds": 15,
        "quiesce_seconds": 2,
        "checkpoint_seconds": 0.8,
        "kill_seconds": 1,
    }
    path = attempt / "launch.json"
    write(path, spec)
    write(Path("/home/worker/current.json"), {"attempt": str(attempt)})
    return Supervisor(Launch.load(attempt, path)).run()


if __name__ == "__main__":
    raise SystemExit(main())
