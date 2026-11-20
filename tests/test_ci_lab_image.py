"""The CI lab worker image: its Dockerfile, offline smoke and build workflow."""

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from scripts import ci_branch_lanes, ci_runner_classes

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = ROOT / "docker/ci-lab/Dockerfile"
SMOKE = ROOT / "docker/ci-lab/smoke.sh"
WORKFLOW = ROOT / ".github/workflows/ci-lab-image.yml"
FAKE_DOCKER = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$DOCKER_LOG"
case " $* " in *" -i "*) cat >> "$DOCKER_STDIN" ;; esac
exit "${DOCKER_EXIT:-0}"
"""


def _instructions() -> list[tuple[str, str]]:
    joined = DOCKERFILE.read_text().replace("\\\n", " ")
    found: list[tuple[str, str]] = []
    for line in joined.splitlines():
        if line.strip():
            instruction, _, rest = line.strip().partition(" ")
            found.append((instruction, " ".join(rest.split())))
    return found


def _runs_as(user: str) -> list[str]:
    current = ""
    runs: list[str] = []
    for instruction, rest in _instructions():
        if instruction == "USER":
            current = rest
        elif instruction == "RUN" and current == user:
            runs.append(rest)
    return runs


def _workflow() -> dict[Any, Any]:
    return yaml.safe_load(WORKFLOW.read_text())


def _fake_docker(tmp_path: Path, exit_code: int = 0) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(FAKE_DOCKER)
    docker.chmod(0o755)
    return {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "DOCKER_LOG": str(tmp_path / "docker.log"),
        "DOCKER_STDIN": str(tmp_path / "docker.stdin"),
        "DOCKER_EXIT": str(exit_code),
    }


def _smoke(tmp_path: Path, *args: str, exit_code: int = 0) -> subprocess.CompletedProcess[str]:
    env = _fake_docker(tmp_path, exit_code)
    return subprocess.run(["bash", str(SMOKE), *args], env=env, capture_output=True, text=True, check=False)


def test_the_worker_image_builds_on_the_dev_runner_image() -> None:
    assert _instructions()[0] == ("FROM", "ghcr.io/the-cloud-clockwork/arc-runner:dev")


def test_the_root_step_adds_bubblewrap_and_removes_sudo_with_every_sudoers_file() -> None:
    (root,) = _runs_as("root")
    assert "apt-get install -y --no-install-recommends bubblewrap" in root
    assert "SUDO_FORCE_REMOVE=yes apt-get purge -y sudo" in root
    assert "gpasswd -d runner sudo" in root
    assert "rm -rf /etc/sudoers /etc/sudoers.d /var/lib/apt/lists/*" in root


def test_the_venv_installs_against_the_lock_whose_digest_it_records() -> None:
    (install,) = _runs_as("runner")
    assert "--constraint .github/test-requirements.lock --excludes .github/test-excludes.txt" in install
    assert '"agentihooks[dev,all] @ file://$(ls /tmp/agentihooks-wheel/agentihooks-*.whl)"' in install
    recorded = "sha256sum .github/test-requirements.lock | cut -d ' ' -f 1 > /opt/agentihooks-venv/lockfile.sha256"
    assert recorded in install
    assert install.index("--constraint .github/test-requirements.lock") < install.index(recorded)


def test_the_build_context_carries_the_lock_the_digest_names() -> None:
    allowed = (ROOT / "docker/ci-lab/Dockerfile.dockerignore").read_text().splitlines()
    assert allowed[0] == "**"
    assert {"!.github/test-requirements.lock", "!.github/test-excludes.txt", "!pyproject.toml"} <= set(allowed)


def test_the_image_runs_the_lab_worker_as_the_runner_user() -> None:
    instructions = _instructions()
    assert instructions[-1] == ("ENTRYPOINT", '["agentihooks", "ci-lab-worker"]')
    assert ("ENV", "PATH=/opt/agentihooks-venv/bin:$PATH") in instructions
    assert [rest for name, rest in instructions if name == "USER"][-1] == "runner"


def test_the_smoke_runs_the_self_test_and_the_image_checks_with_no_network(tmp_path: Path) -> None:
    result = _smoke(tmp_path, "worker:candidate", "ab12")
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "docker.log").read_text().splitlines() == [
        "run --rm --network none worker:candidate --self-test",
        "run --rm -i --network none -e LOCK_DIGEST=ab12 --entrypoint bash worker:candidate -s",
    ]
    checks = (tmp_path / "docker.stdin").read_text()
    assert "bwrap --version\n" in checks
    assert 'if [[ "$(cat /opt/agentihooks-venv/lockfile.sha256)" != "$LOCK_DIGEST" ]]; then\n' in checks
    assert "if [[ -e /etc/sudoers || -e /etc/sudoers.d ]]; then\n" in checks
    assert (
        "/opt/agentihooks-venv/bin/python -c 'import scripts.ci_lab.contract, scripts.ci_lab.executors, "
        "scripts.ci_lab.run_tests, scripts.ci_lab.pricing, pytest, xdist"
    ) in checks


def test_the_image_checks_refuse_an_image_that_still_has_sudo(tmp_path: Path) -> None:
    assert _smoke(tmp_path, "worker:candidate", "ab12").returncode == 0
    fake = tmp_path / "bin/sudo"
    fake.write_text("#!/usr/bin/env bash\n")
    fake.chmod(0o755)
    checks = (tmp_path / "docker.stdin").read_text()
    env = {**os.environ, "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}"}
    result = subprocess.run(["bash", "-s"], input=checks, env=env, capture_output=True, text=True, check=False)
    assert result.returncode == 1
    assert result.stderr == "sudo is still installed\n"


def test_the_smoke_stops_when_the_self_test_fails(tmp_path: Path) -> None:
    result = _smoke(tmp_path, "worker:candidate", "ab12", exit_code=3)
    assert result.returncode == 3
    assert (tmp_path / "docker.log").read_text().splitlines() == [
        "run --rm --network none worker:candidate --self-test"
    ]


@pytest.mark.parametrize("args", [(), ("worker:candidate",)])
def test_the_smoke_needs_an_image_and_the_lock_digest(tmp_path: Path, args: tuple[str, ...]) -> None:
    result = _smoke(tmp_path, *args)
    assert result.returncode != 0
    assert " usage: smoke.sh IMAGE LOCK_DIGEST\n" in result.stderr
    assert not (tmp_path / "docker.log").exists()


def test_the_workflow_builds_on_dev_pushes_to_its_inputs_a_daily_refresh_and_dispatch() -> None:
    on = _workflow()[True]
    assert on["push"]["branches"] == ["dev", "diffcheck/ci-lab-image-*"]
    assert {"docker/ci-lab/**", ".github/workflows/ci-lab-image.yml", ".github/test-requirements.lock"} <= set(
        on["push"]["paths"]
    )
    assert on["schedule"] == [{"cron": "47 14 * * *"}]
    assert "workflow_dispatch" in on


def test_a_dev_push_builds_on_its_own_group_and_every_other_run_queues_on_a_shared_lane() -> None:
    workflow = _workflow()
    dev = "github.event_name == 'push' && github.ref == 'refs/heads/dev'"
    assert workflow["concurrency"]["group"].startswith(f"${{{{ {dev} && format('ci-lab-image-{{0}}', github.ref) || ")
    assert workflow["concurrency"]["queue"] == f"${{{{ {dev} && 'single' || 'max' }}}}"
    assert workflow["concurrency"]["cancel-in-progress"] is False
    assert ci_branch_lanes.problems(workflow) == []


def test_the_daily_refresh_rebuilds_on_dev() -> None:
    refresh = _workflow()["jobs"]["refresh"]
    assert refresh["if"] == "github.event_name == 'schedule'"
    assert refresh["permissions"] == {"actions": "write"}
    (step,) = refresh["steps"]
    assert step["run"] == 'gh workflow run ci-lab-image.yml --repo "$GITHUB_REPOSITORY" --ref dev'


def test_the_image_job_smokes_the_candidate_before_it_publishes_dev_and_the_short_sha(tmp_path: Path) -> None:
    job = _workflow()["jobs"]["image"]
    assert job["if"] == "github.event_name != 'schedule'"
    assert job["permissions"] == {"contents": "read", "packages": "write"}
    checkout, guard, login, build, smoke, publish, release = job["steps"]
    assert checkout == {
        "uses": "actions/checkout@v4",
        "with": {"fetch-depth": "${{ github.event_name == 'release' && '0' || '1' }}", "persist-credentials": False},
    }
    assert guard["if"] == release["if"] == "github.event_name == 'release'"
    assert login["uses"] == "docker/login-action@v3"
    assert '--file docker/ci-lab/Dockerfile --tag "$CANDIDATE" .' in build["run"]
    digest = "$(sha256sum .github/test-requirements.lock | cut -d ' ' -f 1)"
    assert smoke["run"] == f'bash docker/ci-lab/smoke.sh "$CANDIDATE" "{digest}"'
    assert "if" not in smoke
    assert publish["if"] == "github.ref == 'refs/heads/dev'"
    env = {**_fake_docker(tmp_path), **job["env"], "CANDIDATE": "candidate", "GITHUB_SHA": "123456789abcdef"}
    subprocess.run(["bash", "-e", "-c", publish["run"]], env=env, check=True)
    image = "ghcr.io/the-cloud-clockwork/agentihooks-ci-lab-worker"
    assert (tmp_path / "docker.log").read_text().splitlines() == [
        f"tag candidate {image}:dev-1234567",
        f"push {image}:dev-1234567",
        f"tag candidate {image}:dev",
        f"push {image}:dev",
    ]


def test_a_release_publishes_the_qualified_candidate_before_the_release_script_moves_its_tags(tmp_path: Path) -> None:
    job = _workflow()["jobs"]["image"]
    release = job["steps"][-1]
    assert release["if"] == "github.event_name == 'release'"
    assert release["env"] == {"RELEASE": "${{ github.event.release.tag_name }}"}
    script = tmp_path / "docker/swarm-node/release.sh"
    script.parent.mkdir(parents=True)
    script.write_text('printf "release.sh %s\\n" "$*" >> "$DOCKER_LOG"\n')
    env = {
        **_fake_docker(tmp_path),
        **job["env"],
        "CANDIDATE": "candidate",
        "GITHUB_SHA": "123456789abcdef",
        "RELEASE": "v2.17.1071",
    }
    subprocess.run(["bash", "-e", "-c", release["run"]], env=env, cwd=tmp_path, check=True)
    image = "ghcr.io/the-cloud-clockwork/agentihooks-ci-lab-worker"
    assert (tmp_path / "docker.log").read_text().splitlines() == [
        f"tag candidate {image}:sha-123456789abcdef",
        f"push {image}:sha-123456789abcdef",
        f"release.sh {image} v2.17.1071 123456789abcdef",
    ]


def test_every_lab_image_job_is_registered_in_the_runner_class_map() -> None:
    table = json.loads((ROOT / ci_runner_classes.TABLE).read_text())
    mine = {key: cls for key, cls in table.items() if key.startswith("ci-lab-image.yml/")}
    assert mine == {"ci-lab-image.yml/image": "standard", "ci-lab-image.yml/refresh": "light"}
    policy = json.loads((ROOT / ".github/workflows/trusted-actors.json").read_text())
    assert ci_runner_classes.check({"ci-lab-image.yml": _workflow()}, mine, policy) == []
