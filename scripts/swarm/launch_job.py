import json
import subprocess
import sys
import time
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

from scripts.swarm.store import AgentRecord, SwarmConfig
from scripts.swarm.tick import Placed, SpawnError

KEY = "launch-jobs"
STATE = "launching"


def start(home: Path, config: SwarmConfig, lane: str, name: str, launch: dict) -> dict:
    from scripts.swarm.runtime import SPAWN_TIMEOUT_S

    folder = home / config.slug / KEY
    folder.mkdir(parents=True, exist_ok=True)
    request, result = folder / f"{name}.request.json", folder / f"{name}.json"
    job = {
        "path": str(result),
        "request": str(request),
        "started_at": int(time.time() * 1000),
        "timeout": SPAWN_TIMEOUT_S,
    }
    request.write_text(
        json.dumps(
            {"home": str(home), "config": asdict(config), "lane": lane, "name": name, "launch": launch, "job": job}
        )
    )
    return job


def spawn(job: dict) -> None:
    request = Path(job["request"])
    with request.with_suffix(".log").open("ab") as errors:
        subprocess.Popen(
            [sys.executable, "-m", "scripts.swarm.launch_job", str(request)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=errors,
            start_new_session=True,
        )


def run(request: Path) -> None:
    from scripts.swarm.runtime import HerdrRuntime

    spec = json.loads(request.read_text())
    config, job = SwarmConfig(**spec["config"]), spec["job"]
    runtime = HerdrRuntime(home=Path(spec["home"]))
    deadline = job["started_at"] / 1000 + job["timeout"]

    def bounded(argv, **kwargs):
        kwargs["timeout"] = min(kwargs["timeout"], max(0, deadline - time.time()))
        return subprocess.run(argv, **kwargs)

    runtime.run = bounded
    try:
        if time.time() >= deadline:
            raise SpawnError("workstation launch job timed out", "ambiguous")
        placed = runtime._launch(config, spec["lane"], name=spec["name"], **spec["launch"])
        if time.time() >= deadline:
            runtime.reap_name(spec["name"])
            raise SpawnError("workstation launch job timed out", "ambiguous", placed.pane_id, harness=placed.harness)
        outcome = {"placed": asdict(placed)}
    except Exception as exc:
        outcome = {
            "error": {
                "message": str(exc),
                "status": getattr(exc, "status", "refused"),
                "pane_id": getattr(exc, "pane_id", ""),
                "harness": getattr(exc, "harness", ""),
            }
        }
    result = Path(job["path"])
    staging = result.with_suffix(".writing")
    staging.write_text(json.dumps(outcome))
    staging.replace(result)


def decided(record: AgentRecord, placed: Placed) -> AgentRecord:
    """The agent record carrying the harness, account, model, effort and profile its launch decided."""
    return replace(
        record,
        harness=placed.harness,
        account=placed.account,
        model=placed.model,
        effort=placed.effort,
        profile=placed.profile,
    )


def pending(store: Any, slug: str, launch: Any, placed: Placed) -> None:
    entry = {"task": launch.task, "placed": asdict(placed)}
    if launch.task.get("handoff"):
        entry["primed"] = launch.primed
    store.redis.hset(store.key(slug, KEY), launch.record.name, json.dumps(entry))
    store.put_agent(slug, decided(launch.record, placed))
    store.record_launch(slug, launch.record, STATE)


def dispatch(run, launch, placed: Placed) -> str | SpawnError:
    try:
        pending(run.store, run.slug, launch, placed)
        spawn(placed.launch_job)
    except Exception as exc:
        run.store.redis.hdel(run.store.key(run.slug, KEY), launch.record.name)
        return SpawnError(str(exc), harness=placed.harness)
    return f"launching {launch.record.name} for {launch.task['id']}"


if __name__ == "__main__":
    run(Path(sys.argv[1]))
