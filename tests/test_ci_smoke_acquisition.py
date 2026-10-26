from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_hive_join_acquires_redis_before_starting_without_another_pull():
    workflow = yaml.safe_load((ROOT / ".github/workflows/swarm-smoke.yml").read_text())
    steps = workflow["jobs"].get("hive-join", workflow["jobs"]["smoke"])["steps"]
    assert any("scripts/packaging/hive-join-smoke.sh" in step.get("run", "").split() for step in steps)
    script = (ROOT / "scripts/packaging/hive-join-smoke.sh").read_text()
    image = "mirror.gcr.io/library/redis:7-alpine"
    pull = f"bash scripts/packaging/pull-runtime-image.sh {image}\n"
    start = next(line for line in script.splitlines() if line.startswith("docker run ") and image in line)
    assert script.index(pull) < script.index(start)
    assert " --pull never " in start


def test_smoke_retags_redis_to_the_ledger_load_registry():
    load = yaml.safe_load((ROOT / ".github/workflows/ledger-load.yml").read_text())["jobs"]["load"]
    registry = load["services"]["redis"]["image"].rsplit(":", 1)[0]
    assert registry == "mirror.gcr.io/library/redis"
    workflow = yaml.safe_load((ROOT / ".github/workflows/swarm-smoke.yml").read_text())
    step = next(step for step in workflow["jobs"]["smoke"]["steps"] if step.get("name") == "Build the swarm image")
    for tag in ("7", "7-alpine"):
        assert (
            f"bash scripts/packaging/pull-runtime-image.sh public.ecr.aws/docker/library/redis:{tag} &&" in step["run"]
        )
        assert f"docker tag public.ecr.aws/docker/library/redis:{tag} {registry}:{tag}" in step["run"]
    assert "pull-runtime-image.sh mirror.gcr.io/library/redis" not in step["run"]


def test_terminal_clients_acquires_every_python_base_before_building():
    workflow = yaml.safe_load((ROOT / ".github/workflows/swarm-smoke.yml").read_text())
    steps = workflow["jobs"].get("terminal-clients", workflow["jobs"]["smoke"])["steps"]
    commands = "\n".join(
        "docker build " if step.get("uses") == "docker/build-push-action@v6" else step.get("run", "") for step in steps
    )
    build = commands.index("docker build ")
    bases = {line.split()[1] for line in (ROOT / "Dockerfile").read_text().splitlines() if line.startswith("FROM ")}
    assert bases
    for base in bases:
        pull = f"bash scripts/packaging/pull-runtime-image.sh {base}\n"
        assert pull in commands
        assert commands.index(pull) < build


def test_smoke_steps_after_the_build_run_only_when_the_build_and_redis_pulls_succeeded():
    steps = yaml.safe_load((ROOT / ".github/workflows/swarm-smoke.yml").read_text())["jobs"]["smoke"]["steps"]
    build = next(i for i, step in enumerate(steps) if step.get("name") == "Build the swarm image")
    assert steps[build]["id"] == "build"
    assert all("if" not in step for step in steps[: build + 1])
    gated = "${{ !cancelled() && steps.build.outcome == 'success' }}"
    after = steps[build + 1 :]
    assert [step["if"] for step in after if "cancelled()" in step.get("if", "")] == [gated] * 4
    assert all(step.get("if") == gated or "()" not in step.get("if", "") for step in after)
