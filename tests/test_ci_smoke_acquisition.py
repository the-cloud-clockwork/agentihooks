from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_hive_join_acquires_redis_before_starting_without_another_pull():
    workflow = yaml.safe_load((ROOT / ".github/workflows/swarm-smoke.yml").read_text())
    steps = workflow["jobs"].get("hive-join", workflow["jobs"]["smoke"])["steps"]
    assert any(step.get("run") == "bash scripts/packaging/hive-join-smoke.sh" for step in steps)
    script = (ROOT / "scripts/packaging/hive-join-smoke.sh").read_text()
    image = "mirror.gcr.io/library/redis:7-alpine"
    pull = f"bash scripts/packaging/pull-runtime-image.sh {image}\n"
    start = next(line for line in script.splitlines() if line.startswith("docker run ") and image in line)
    assert script.index(pull) < script.index(start)
    assert " --pull never " in start


def test_terminal_clients_acquires_every_python_base_before_building():
    workflow = yaml.safe_load((ROOT / ".github/workflows/swarm-smoke.yml").read_text())
    steps = workflow["jobs"].get("terminal-clients", workflow["jobs"]["smoke"])["steps"]
    commands = "\n".join(step.get("run", "") for step in steps)
    build = commands.index("docker build ")
    bases = {line.split()[1] for line in (ROOT / "Dockerfile").read_text().splitlines() if line.startswith("FROM ")}
    assert bases
    for base in bases:
        pull = f"bash scripts/packaging/pull-runtime-image.sh {base}\n"
        assert pull in commands
        assert commands.index(pull) < build
