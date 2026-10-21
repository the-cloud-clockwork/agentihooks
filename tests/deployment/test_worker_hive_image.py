import os
import re
import subprocess
import tomllib
from pathlib import Path

import pytest
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
    assert "--build-arg" not in published
    assert f"ARG WORKER_IMAGE={BASE}\n" in (ROOT / "deploy/worker/Dockerfile").read_text()
    assert "-f docker/swarm-node/Dockerfile" in rebuilt_base
    assert "--build-arg WORKER_IMAGE=local/worker-base:dev" in rebuilt_hive


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
