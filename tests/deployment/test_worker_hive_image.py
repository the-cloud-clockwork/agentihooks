import os
import re
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]
PROOF = ROOT / "deploy/worker/kind-proof.sh"
CURRENT = ROOT / "deploy/worker/base-current.sh"
BASE = "ghcr.io/the-cloud-clockwork/agentihooks-worker:dev"
INPUTS = "docker/swarm-node scripts/swarm_v2/worker_image.py"


def builds():
    joined = re.sub(r"\\\n\s*", " ", PROOF.read_text())
    return [line.strip() for line in joined.splitlines() if "docker build" in line]


def judge(tmp_path, *, diff=0, run=0):
    tools = tmp_path / "bin"
    tools.mkdir()
    log = tmp_path / "commands"
    (tools / "docker").write_text(
        '#!/usr/bin/env bash\nprintf "docker %s\\n" "$*" >> "$COMMAND_LOG"\n'
        'if [[ "$1" == run ]]; then\n  [[ "$FAKE_RUN" == 0 ]] || exit "$FAKE_RUN"\n  echo rev123\nfi\n'
    )
    (tools / "git").write_text(
        '#!/usr/bin/env bash\nprintf "git %s\\n" "$*" >> "$COMMAND_LOG"\n[[ "$1" != diff ]] || exit "$FAKE_DIFF"\n'
    )
    for tool in tools.iterdir():
        tool.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{tools}:{os.environ['PATH']}",
        "COMMAND_LOG": str(log),
        "FAKE_DIFF": str(diff),
        "FAKE_RUN": str(run),
    }
    result = subprocess.run(["bash", str(CURRENT), BASE], env=env, capture_output=True, text=True)
    return result.returncode, log.read_text().splitlines()


def test_kind_proof_layers_only_the_hive_on_a_current_base():
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


def test_base_is_current_when_its_inputs_match_the_published_revision(tmp_path):
    status, commands = judge(tmp_path)
    assert status == 0
    assert commands[0] == f"docker pull -q --platform linux/amd64 {BASE}"
    assert "git fetch -q --depth=1 origin rev123" in commands
    assert commands[-1] == f"git diff --quiet rev123 HEAD -- {INPUTS}"


def test_base_is_not_current_when_an_input_changed(tmp_path):
    status, commands = judge(tmp_path, diff=1)
    assert status == 1
    assert commands[-1] == f"git diff --quiet rev123 HEAD -- {INPUTS}"


def test_base_is_not_current_when_its_revision_cannot_be_read(tmp_path):
    status, commands = judge(tmp_path, run=125)
    assert status == 125
    assert not any(command.startswith("git") for command in commands)
