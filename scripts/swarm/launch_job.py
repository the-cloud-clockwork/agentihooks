import json
import subprocess
import sys
import time
from dataclasses import asdict, replace
from pathlib import Path

from scripts.swarm.store import SwarmConfig
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


def read(job: dict, now_ms: int) -> Placed | None:
    path = Path(job["path"])
    if not path.exists():
        if now_ms - job["started_at"] >= job["timeout"] * 1000:
            raise SpawnError("workstation launch job timed out", "ambiguous")
        return None
    outcome = json.loads(path.read_text())
    if "error" in outcome:
        error = outcome["error"]
        raise SpawnError(error["message"], error["status"], error["pane_id"], harness=error["harness"])
    return Placed(**outcome["placed"])


def pending(store, slug: str, launch, placed: Placed) -> None:
    store.redis.hset(
        store.key(slug, KEY), launch.record.name, json.dumps({"task": launch.task, "placed": asdict(placed)})
    )
    store.record_launch(slug, launch.record, STATE)


def dispatch(run, launch, placed: Placed) -> str | SpawnError:
    try:
        pending(run.store, run.slug, launch, placed)
        spawn(placed.launch_job)
    except Exception as exc:
        run.store.redis.hdel(run.store.key(run.slug, KEY), launch.record.name)
        return SpawnError(str(exc), harness=placed.harness)
    return f"launching {launch.record.name} for {launch.task['id']}"


def completed(picked: Placed, placed: Placed) -> Placed:
    return replace(
        picked,
        pane_id=placed.pane_id,
        harness=placed.harness,
        account=placed.account,
        model=placed.model,
        effort=placed.effort,
        placement=placed.placement,
        profile=placed.profile,
        profile_decision={**picked.profile_decision, **placed.profile_decision},
        launched_at=placed.launched_at,
        launch_timings=placed.launch_timings,
        conversation_id=placed.conversation_id,
        route_kind="api" if placed.account == "api" else picked.route_kind,
        launch_job={},
    )


def collect(run) -> tuple[list[str], set[str]]:
    from scripts.swarm import tick as engine
    from scripts.swarm_v2.runtime import codex

    key, actions, waiting = run.store.key(run.slug, KEY), [], set()
    agents = {agent.name: agent for agent in run.store.agents(run.slug)}
    for name, raw in run.store.redis.hgetall(key).items():
        saved = json.loads(raw)
        agent = agents.get(name)
        if agent is None:
            run.store.redis.hdel(key, name)
            continue
        task, picked = saved["task"], Placed(**saved["placed"])
        try:
            placed = read(picked.launch_job, run.now_ms)
            if placed is None:
                run.store.refresh(run.slug, agent.task, agent.name, engine.LEASE_MS)
                waiting.add(name)
                continue
            placed = completed(picked, placed)
            if agent.state != "finished":
                placed = codex.complete(run.config, name, placed, run.runtime.reap_name)
                launched = codex.admit_legacy(run.store, run.slug, engine.placed_record(agent, placed), run.runtime)
        except Exception as exc:
            error = exc if isinstance(exc, SpawnError) else SpawnError(str(exc))
            if not error.harness:
                error.harness = picked.harness
            if not error.pane_id:
                from scripts.swarm.runtime import _opened_pane

                error.pane_id = _opened_pane(name, picked.launch_job["started_at"])
            run.runtime.reap_name(name)
            actions += engine._spawn_failed(
                run.slug, run.store, run.ledger, run.runtime, run.rows, engine.Failed(agent, task, error), run.now_ms
            )
        else:
            if agent.state == "finished":
                actions.append(
                    engine._finished_launch(run.slug, run.store, run.doc, engine.placed_record(agent, placed))
                )
            else:
                actions += engine._started(
                    run.slug,
                    run.config,
                    run.store,
                    run.ledger,
                    run.doc,
                    engine.Started(agent, task, placed, launched),
                    run.now_ms,
                )
        run.store.redis.hdel(key, name)
    return actions, waiting


if __name__ == "__main__":
    run(Path(sys.argv[1]))
