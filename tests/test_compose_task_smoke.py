from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = "scripts/packaging/compose-task-smoke.sh"


def smoke():
    return (ROOT / SCRIPT).read_text()


def test_the_swarm_image_job_runs_the_compose_task_smoke_on_the_built_image():
    steps = yaml.safe_load((ROOT / ".github/workflows/swarm-smoke.yml").read_text())["jobs"]["smoke"]["steps"]
    parallel = next(step for step in steps if "scripts/packaging/swarm-smoke-parallel.sh" in step.get("run", ""))
    assert SCRIPT in parallel["run"].split()


def test_the_compose_task_smoke_starts_the_stack_with_nats_and_removes_it_with_its_volumes():
    script = smoke()
    assert "trap cleanup EXIT" in script
    assert "docker compose down --volumes" in script
    assert 'docker compose up --pull never "${build[@]}" --wait --wait-timeout 180 redis nats swarm' in script
    assert script.index("bash scripts/packaging/pull-runtime-image.sh mirror.gcr.io/library/nats:2.15.1-alpine") < (
        script.index("docker compose up")
    )
    assert 'left="$(docker ps --all --quiet --filter "$owned")$(docker volume ls --quiet --filter "$owned")"' in script
    assert 'left+="$(docker network ls --quiet --filter "$owned")"' in script


def test_the_compose_task_smoke_settles_one_task_to_done_through_the_hub_on_nats():
    script = smoke()
    assert "readiness.publish(bus" in script
    assert "agentihooks hub run --once" in script
    assert "execution_events.publish(bus" in script
    assert '["agentihooks", "swarm", slug, "done"' in script
    assert script.index("agentihooks hub run --once") < script.index("printf 'Compose task smoke passed")
    gate = (
        """if [[ $settled != '{"task": "done", "claim": null, "execution": "finished", "agent": false, """
        """"spoke": "exited"}' ]]; then\n"""
    )
    assert gate in script
    assert script.index(gate) < script.index("printf 'Compose task smoke passed")
