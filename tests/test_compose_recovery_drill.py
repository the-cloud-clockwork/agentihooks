from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = "scripts/packaging/compose-recovery-drill.sh"


def drill():
    return (ROOT / SCRIPT).read_text()


def test_the_swarm_image_job_runs_the_recovery_drill_on_the_built_image():
    steps = yaml.safe_load((ROOT / ".github/workflows/swarm-smoke.yml").read_text())["jobs"]["smoke"]["steps"]
    parallel = next(step for step in steps if "scripts/packaging/swarm-smoke-parallel.sh" in step.get("run", ""))
    assert SCRIPT in parallel["run"].split()


def test_the_recovery_drill_binds_ports_apart_from_the_other_compose_scenarios():
    script = drill()
    assert 'SWARM_PORT="${SWARM_PORT:-38765}"' in script
    assert 'SWARM_REDIS_PORT="${SWARM_REDIS_PORT:-36379}"' in script
    assert "trap cleanup EXIT" in script
    assert "docker compose down --volumes" in script


def test_the_recovery_drill_restores_the_cold_backup_before_the_hub_reconciles():
    script = drill()
    backup = script.index("volume 'tar -C /data -cf /backup/jetstream.tar .'")
    restore = script.index("volume 'find /data -mindepth 1 -delete && tar -C /data -xf /backup/jetstream.tar'")
    assert script.index("drill seed t1 t2 t3 t4") < backup < script.index("drill done t1") < restore
    assert restore < script.index("drill die t2 t3") < script.index('settle "$want"')
    assert script.count("docker compose stop nats") == 2


def test_the_recovery_drill_requires_every_task_done_once_per_launch_with_no_claim_or_agent():
    script = drill()
    for task, launches in (("t1", 1), ("t2", 2), ("t3", 2), ("t4", 1), ("t5", 1)):
        assert f'"{task}": {{"ledger": "done", "claim": null, "launches": {launches}}}' in script
    assert '"agents": []}' in script
    assert script.index('settle "$want"') < script.index("printf 'Recovery drill passed")
