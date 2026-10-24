import os
import re
import subprocess
import tomllib
from pathlib import Path

import pytest
import yaml
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]
PROOF = ROOT / "deploy/worker/kind-proof.sh"
CURRENT = ROOT / "deploy/worker/base-current.sh"
BASE = "ghcr.io/the-cloud-clockwork/agentihooks-worker:dev"
INPUTS = "docker/swarm-node scripts/swarm_v2/worker_image.py profiles docs/swarm-v2/schemas"
WHEEL = {"pyproject.toml", "README.md", "hooks/", "scripts/"}


def builds():
    joined = re.sub(r"\\\n\s*", " ", PROOF.read_text())
    return [line.strip() for line in joined.splitlines() if "docker build" in line]


def judge(tmp_path, *, diff=0, pull=0, run=0, fetch=0):
    tools = tmp_path / "bin"
    tools.mkdir()
    log = tmp_path / "commands"
    (tools / "docker").write_text(
        '#!/usr/bin/env bash\nprintf "docker %s\\n" "$*" >> "$COMMAND_LOG"\n'
        'if [[ "$1" == pull ]]; then exit "$FAKE_PULL"; fi\n'
        'if [[ "$1" == run ]]; then\n  [[ "$FAKE_RUN" == 0 ]] || exit "$FAKE_RUN"\n  echo rev123\nfi\n'
    )
    (tools / "git").write_text(
        '#!/usr/bin/env bash\nprintf "git %s\\n" "$*" >> "$COMMAND_LOG"\n'
        'if [[ "$1" == fetch ]]; then exit "$FAKE_FETCH"; fi\n'
        '[[ "$1" != diff ]] || exit "$FAKE_DIFF"\n'
    )
    for tool in tools.iterdir():
        tool.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{tools}:{os.environ['PATH']}",
        "COMMAND_LOG": str(log),
        "FAKE_DIFF": str(diff),
        "FAKE_PULL": str(pull),
        "FAKE_RUN": str(run),
        "FAKE_FETCH": str(fetch),
    }
    result = subprocess.run(["bash", str(CURRENT), BASE], env=env, capture_output=True, text=True)
    return result.returncode, log.read_text().splitlines(), result.stdout


def test_kind_proof_layers_only_the_hive_on_a_current_base():
    assert len(builds()) == 3
    published, rebuilt_base, rebuilt_hive = builds()
    assert "--pull" not in published
    assert "-f deploy/worker/Dockerfile" in published
    assert "--build-arg WORKER_IMAGE" not in published
    assert f"ARG WORKER_IMAGE={BASE}\n" in (ROOT / "deploy/worker/Dockerfile").read_text()
    assert "-f docker/swarm-node/Dockerfile" in rebuilt_base
    assert "--build-arg WORKER_IMAGE=local/worker-base:dev" in rebuilt_hive
    for hive in (published, rebuilt_hive):
        assert '--build-arg AGENTIHOOKS_REF="${AGENTIHOOKS_REF:-dev}"' in hive


def worker_stage(name):
    dockerfile = (ROOT / "deploy/worker/Dockerfile").read_text()
    start = dockerfile.rindex("\nFROM ", 0, dockerfile.index(f" AS {name}\n")) + 1
    return dockerfile[start : dockerfile.index("\nFROM ", start)]


def test_hive_image_installs_agentihooks_cloned_from_the_chosen_branch():
    dockerfile = (ROOT / "deploy/worker/Dockerfile").read_text()
    assert "ARG AGENTIHOOKS_SOURCE=git" in dockerfile[: dockerfile.index("\nFROM ")].splitlines()
    clone = worker_stage("source-git")
    assert "ARG AGENTIHOOKS_REF=dev\n" in clone
    assert "ADD https://github.com/the-cloud-clockwork/agentihooks.git#${AGENTIHOOKS_REF} /build\n" in clone
    assert "COPY" not in clone
    wheel = worker_stage("wheel")
    assert wheel.startswith("FROM source-${AGENTIHOOKS_SOURCE} AS wheel\n")
    assert "COPY" not in wheel
    assert "ADD" not in wheel


def test_hive_image_can_install_agentihooks_from_the_local_build_context():
    local = worker_stage("source-local")
    assert "ADD" not in local
    sources = {s for line in re.findall(r"^COPY (.+) \S+$", local, re.M) for s in line.split()}
    assert sources == {"pyproject.toml", "README.md", "hooks", "scripts", "profiles"}
    allowed = (ROOT / "deploy/worker/Dockerfile.dockerignore").read_text().splitlines()
    for source in sources:
        assert f"!{source}" in allowed or f"!{source}/" in allowed, source


def test_kind_proof_rebuilds_the_base_only_when_it_is_not_current():
    current, _, stale = PROOF.read_text().partition("\nelse\n")
    assert f"if bash deploy/worker/base-current.sh {BASE}; then" in current
    assert "docker/swarm-node/Dockerfile" not in current
    assert "docker/swarm-node/Dockerfile" in stale.split("\nfi\n")[0]


def test_base_lock_satisfies_every_requirement_of_the_workstation_install():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    wanted = [Requirement(spec) for spec in project["dependencies"] + project["optional-dependencies"]["all"]]
    lock = (ROOT / "docker/swarm-node/requirements.lock").read_text()
    pins = {
        canonicalize_name(name): version
        for name, version in re.findall(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==(\S+)", lock, re.M)
    }
    assert {"croniter", "boto3", "psycopg2-binary", "redis"} <= {canonicalize_name(r.name) for r in wanted}
    unmet = [
        str(r)
        for r in wanted
        if not r.specifier.contains(pins.get(canonicalize_name(r.name), "0.0.0a0"), prereleases=True)
    ]
    assert unmet == []


def test_kind_proof_checks_init_renders_the_engineer_profile_before_the_cluster_wait():
    lines = PROOF.read_text().splitlines()
    check = (
        'docker run --rm --tmpfs /home/worker:uid=10001,gid=10001 --entrypoint bash "$image" -c '
        "'agentihooks init --profile engineer && "
        'grep -qx "<!-- profile: engineer -->" "$HOME/.claude/CLAUDE.md"\' > "$out/init.txt"'
    )
    assert lines[:2] == ["#!/usr/bin/env bash", "set -euo pipefail"]
    assert lines.count(check) == 1
    assert lines.index(check) < lines.index('wait "$cluster_pid"')


def test_kind_proof_checks_the_bundle_system_tools_install_into_the_worker_home():
    tools = [line for line in PROOF.read_text().splitlines() if '> "$out/tools.txt"' in line]
    assert len(tools) == 1
    for check in (
        "node --version",
        "npm --version",
        "uv --version",
        "gh --version",
        'test "$(npm config get prefix)" = "$HOME/.local"',
        '[[ ":$PATH:" == *":$HOME/.local/bin:"* ]]',
    ):
        assert f"&& {check} " in tools[0], check


def test_base_inputs_cover_every_base_source_the_wheel_does_not_replace():
    dockerfile = (ROOT / "docker/swarm-node/Dockerfile").read_text()
    sources = {s for line in re.findall(r"^COPY (?:--\S+ )*(.+) \S+$", dockerfile, re.M) for s in line.split()}
    assert sources - WHEEL
    for source in sources - WHEEL:
        assert any(source.startswith(path) for path in INPUTS.split()), source


def test_base_is_current_when_its_inputs_match_the_published_revision(tmp_path):
    status, commands, _ = judge(tmp_path)
    assert status == 0
    assert commands[0] == f"docker pull -q --platform linux/amd64 {BASE}"
    assert "git fetch -q --depth=1 origin rev123" in commands
    assert commands[-1] == f"git diff --quiet rev123 HEAD -- {INPUTS}"


def test_base_is_stale_when_an_input_changed(tmp_path):
    status, commands, output = judge(tmp_path, diff=1)
    assert status == 1
    assert commands[-1] == f"git diff --quiet rev123 HEAD -- {INPUTS}"
    assert "inputs changed since rev123" in output


@pytest.mark.parametrize(
    ("failure", "reason"),
    [
        ({"pull": 1}, "revision cannot be read"),
        ({"run": 125}, "revision cannot be read"),
        ({"fetch": 128}, "revision rev123 cannot be fetched"),
        ({"diff": 128}, "inputs cannot be compared with revision rev123"),
    ],
)
def test_base_is_undetermined_with_a_visible_reason_when_the_comparison_fails(tmp_path, failure, reason):
    status, _, output = judge(tmp_path, **failure)
    assert status == 2
    assert output.startswith("::warning title=Worker hive base::")
    assert reason in output


def test_the_hive_image_carries_the_pyproject_ruff_pin_and_the_kind_proof_runs_it():
    assert "&& ruff --version &&" in PROOF.read_text()
    assert "r.startswith('ruff==')" in (ROOT / "deploy/worker/Dockerfile").read_text()


def hive_jobs():
    return yaml.safe_load((ROOT / ".github/workflows/hive-worker.yml").read_text())["jobs"]


def test_the_kind_job_publishes_the_proven_image_on_dev_pushes_after_the_chart_proof():
    jobs = hive_jobs()
    kind = jobs["kind"]
    assert "publish" not in jobs
    assert kind["needs"] == "contracts"
    names = [step.get("name", step.get("uses", "")) for step in kind["steps"]]
    assert names.index("Publish the proven hive worker image") > names.index("docker/login-action@v3")
    assert names.index("docker/login-action@v3") > names.index(
        "Build the worker image while kind starts, then prove registration and order acknowledgement"
    )
    dev_push = "github.event_name == 'push' && github.ref == 'refs/heads/dev'"
    assert [step.get("if") for step in kind["steps"] if "ghcr.io" in str(step)] == [dev_push, dev_push]


def test_a_release_builds_the_hive_worker_on_its_released_worker_and_moves_release_only_when_newest():
    jobs = yaml.safe_load((ROOT / ".github/workflows/swarm-node-image.yml").read_text())["jobs"]
    hive = jobs["hive"]
    assert hive["if"] == "github.event_name == 'release'"
    assert hive["needs"] == "image"
    assert jobs["image"]["outputs"]["latest"] == "${{ steps.main.outputs.latest }}"
    assert hive["env"]["IMAGE"] == "ghcr.io/the-cloud-clockwork/agentihooks-hive-worker"
    steps = {step.get("name", step.get("uses")): step for step in hive["steps"]}
    build = steps["Build the hive worker on the released worker image"]["run"]
    assert 'WORKER_IMAGE="ghcr.io/the-cloud-clockwork/agentihooks-worker:${RELEASE#v}"' in build
    assert 'AGENTIHOOKS_REF="$GITHUB_SHA"' in build
    names = list(steps)
    assert names.index("Prove the hive worker installs its profile") < names.index(
        "Publish the release version and move the release tag when it is the newest"
    )
    publish = steps["Publish the release version and move the release tag when it is the newest"]
    assert publish["env"]["LATEST"] == "${{ needs.image.outputs.latest }}"
    assert 'if [[ "$LATEST" == true ]]; then' in publish["run"]


def test_the_proven_image_skips_the_artifact_round_trip():
    assert "docker save" not in PROOF.read_text()
    assert all("image.tar" not in str(step) for job in hive_jobs().values() for step in job["steps"])


def test_kind_proof_pulls_the_base_and_redis_into_the_node_while_the_hive_builds():
    lines = PROOF.read_text().splitlines()
    pulls = [
        f'  docker exec "$cluster-control-plane" crictl pull {image}'
        for image in (BASE, "mirror.gcr.io/library/redis:7-alpine")
    ]
    start = lines.index("{")
    assert lines[start + 1].startswith('  kind create cluster --name "$cluster"')
    assert lines[start + 2 : start + 4] == pulls
    assert lines[start + 4] == '} > "$out/cluster.log" 2>&1 &'
    load = lines.index('kind load docker-image "$image" --name "$cluster" &')
    assert lines.index('wait "$cluster_pid"') < load < lines.index('wait "$load_pid"')
    assert lines.index('wait "$load_pid"') < lines.index("kubectl apply -f - <<EOF")


def test_kind_proof_ends_the_drain_hold_once_the_hive_reads_draining():
    text = PROOF.read_text()
    assert "sleep 45" not in text
    lines = text.splitlines()
    restart = lines.index('kubectl rollout restart "deployment/$deployment"')
    assert lines[restart + 1 : restart + 3] == ["hold_agent draining", "sleep 5"]
    assert "        if registry.show(store.redis, os.environ['HIVE'])['draining'] == 'yes':" in lines
