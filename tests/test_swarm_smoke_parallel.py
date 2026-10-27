import re
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts/packaging/swarm-smoke-parallel.sh"
SCENARIOS = [
    "scripts/packaging/hive-launch-smoke.sh",
    "scripts/packaging/swarm-smoke.sh",
    "scripts/packaging/hive-join-smoke.sh",
    "scripts/packaging/compose-hive-smoke.sh",
    "scripts/packaging/compose-task-smoke.sh",
    "scripts/packaging/compose-recovery-drill.sh",
    "scripts/packaging/swarm-init-smoke.sh",
]


def run(tmp_path, *scenarios):
    return subprocess.run(
        ["bash", str(RUNNER), *scenarios],
        env={"PATH": "/usr/bin:/bin", "RUNNER_TEMP": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=60,
    )


def rendezvous(tmp_path, mine, theirs):
    script = tmp_path / f"{mine}.sh"
    script.write_text(
        f"""touch {tmp_path}/{mine}.started
for _ in $(seq 50); do
  [[ -e {tmp_path}/{theirs}.started ]] && echo "{mine} saw {theirs}" && exit 0
  sleep 0.1
done
echo "{mine} ran alone" >&2
exit 1
"""
    )
    return str(script)


def test_scenarios_run_at_the_same_time(tmp_path):
    result = run(tmp_path, rendezvous(tmp_path, "a", "b"), rendezvous(tmp_path, "b", "a"))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "a saw b" in result.stdout and "b saw a" in result.stdout


def test_one_failed_scenario_fails_the_step_and_every_scenario_reports(tmp_path):
    passing = tmp_path / "passing.sh"
    passing.write_text("echo passing output\n")
    failing = tmp_path / "failing.sh"
    failing.write_text("echo failing output >&2\nexit 3\n")
    result = run(tmp_path, str(failing), str(passing))
    assert result.returncode == 1
    assert re.search(rf"::group::{re.escape(str(failing))} failed with exit 3 in \d+s\nfailing output\n", result.stdout)
    assert re.search(rf"::group::{re.escape(str(passing))} passed in \d+s\npassing output\n", result.stdout)
    assert f"::error title={failing}::failed with exit 3" in result.stdout
    assert f"::error title={passing}" not in result.stdout


def test_scenarios_sharing_a_file_name_keep_their_own_results(tmp_path):
    scenarios = []
    for name, status in (("one", 0), ("two", 4)):
        folder = tmp_path / name
        folder.mkdir()
        script = folder / "scenario.sh"
        script.write_text(f"echo {name} output\nexit {status}\n")
        scenarios.append(str(script))
    result = run(tmp_path, *scenarios)
    assert result.returncode == 1
    assert re.search(rf"::group::{re.escape(scenarios[0])} passed in \d+s\none output\n", result.stdout)
    assert re.search(rf"::group::{re.escape(scenarios[1])} failed with exit 4 in \d+s\ntwo output\n", result.stdout)


def test_the_swarm_image_job_runs_every_scenario_in_one_parallel_step_on_the_built_image():
    job = yaml.safe_load((ROOT / ".github/workflows/swarm-smoke.yml").read_text())["jobs"]["smoke"]
    steps = job["steps"]
    build = next(
        i
        for i, step in enumerate(steps)
        if step.get("run", "").startswith("docker build -t ") or step.get("uses") == "docker/build-push-action@v6"
    )
    parallel = [i for i, step in enumerate(steps) if "scripts/packaging/swarm-smoke-parallel.sh" in step.get("run", "")]
    assert len(parallel) == 1 and parallel[0] > build
    assert steps[parallel[0]]["run"].split()[2:] == SCENARIOS
    step = steps[build]
    if "run" in step:
        image = step["run"].split()[3]
    else:
        assert step["with"]["context"] == "."
        assert step["with"]["load"] is True
        image = step["with"]["tags"]
    assert job["env"]["SWARM_SMOKE_IMAGE"] == image
    for scenario in SCENARIOS:
        assert not any(step.get("run") == f"bash {scenario}" for step in steps)
    python = next(i for i, step in enumerate(steps) if step.get("uses", "").startswith("actions/setup-python@"))
    assert python < parallel[0]
    assert steps[python]["with"]["python-version"] == "3.12"


def test_the_compose_install_scenario_leaves_the_default_ports_to_swarm_init():
    script = (ROOT / "scripts/packaging/swarm-smoke.sh").read_text()
    compose = (ROOT / "compose.yaml").read_text()
    taken = set()
    for other in ("compose-hive-smoke.sh", "compose-task-smoke.sh", "compose-recovery-drill.sh"):
        taken |= set(
            re.findall(r'SWARM_\w*PORT="\$\{SWARM_\w*PORT:-(\d+)\}"', (ROOT / "scripts/packaging" / other).read_text())
        )
    for name, default in (("SWARM_PORT", "8765"), ("SWARM_REDIS_PORT", "6379")):
        assert f"${{{name}:-{default}}}" in compose
        bound = re.search(rf'{name}="\$\{{{name}:-(\d+)\}}"', script)
        assert bound and bound.group(1) != default and bound.group(1) not in taken
    assert re.search(r"^export [^\n]*\bSWARM_PORT\b[^\n]*\bSWARM_REDIS_PORT\b", script, re.MULTILINE)


def test_the_two_compose_scenarios_bind_different_host_ports():
    script = (ROOT / "scripts/packaging/compose-hive-smoke.sh").read_text()
    compose = (ROOT / "compose.yaml").read_text()
    for name, default in (("SWARM_PORT", "8765"), ("SWARM_HIVE_PORT", "8770"), ("SWARM_REDIS_PORT", "6379")):
        assert f"${{{name}:-{default}}}" in compose
        bound = re.search(rf'{name}="\$\{{{name}:-(\d+)\}}"', script)
        assert bound and bound.group(1) != default
