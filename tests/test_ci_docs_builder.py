import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[1]


def test_documentation_builder_is_acquired_after_selection():
    jobs = yaml.safe_load((_ROOT / ".github/workflows/test.yml").read_text())["jobs"]
    for job in jobs.values():
        for step in job.get("steps", []):
            assert not step.get("uses", "").startswith("actions/jekyll-build-pages@")
    steps = next(
        job["steps"]
        for job in jobs.values()
        if any(step.get("name") == "Select docs build" for step in job.get("steps", []))
    )
    selection = next(i for i, step in enumerate(steps) if step.get("id") == "docs")
    build = next(i for i, step in enumerate(steps) if step.get("name") == "Build docs with Jekyll")
    assert selection < build
    assert steps[build]["if"] == "${{ !cancelled() && steps.docs.outputs.build == 'true' }}"
    assert "docker run" in steps[build]["run"]
    upload = next(step for step in steps if step.get("name") == "Retain the rendered documentation")
    assert upload["if"] == steps[build]["if"]
    assert upload["with"]["path"] == "_site/"
    assert upload["with"]["if-no-files-found"] == "error"


@pytest.mark.parametrize("status", [0, 17])
def test_documentation_builder_preserves_action_inputs_and_failure(tmp_path, status):
    jobs = yaml.safe_load((_ROOT / ".github/workflows/test.yml").read_text())["jobs"]
    build = next(
        step for job in jobs.values() for step in job.get("steps", []) if step.get("name") == "Build docs with Jekyll"
    )
    docker = tmp_path / "docker"
    docker.write_text(
        "#!/usr/bin/env python3\nimport json, os, sys\n"
        'print(json.dumps({"args": sys.argv[1:], "env": dict(os.environ)}))\n'
        'sys.exit(int(os.environ["DOCKER_STATUS"]))\n'
    )
    docker.chmod(0o755)
    env = os.environ | {name: str(value) for name, value in build["env"].items()}
    env.update(
        PATH=f"{tmp_path}:{env['PATH']}",
        DOCKER_STATUS=str(status),
        GITHUB_WORKSPACE=str(tmp_path / "checkout"),
        GITHUB_REPOSITORY="owner/repo",
        GITHUB_API_URL="https://api.github.com",
    )
    result = subprocess.run(["bash", "-e", "-c", build["run"]], env=env, text=True, capture_output=True, check=False)
    assert result.returncode == status, result.stderr
    call = json.loads(result.stdout)
    assert call["args"][-1] == "ghcr.io/actions/jekyll-build-pages:v1.0.13"
    assert f"{env['GITHUB_WORKSPACE']}:/github/workspace" in call["args"]
    for name, value in {
        "INPUT_SOURCE": "./",
        "INPUT_DESTINATION": "./_site",
        "INPUT_FUTURE": "false",
        "INPUT_VERBOSE": "true",
        "INPUT_BUILD_REVISION": "${{ github.sha }}",
        "INPUT_TOKEN": "",
        "GITHUB_REPOSITORY": "owner/repo",
    }.items():
        assert name in call["args"]
        assert call["env"][name] == value
    assert "GITHUB_WORKSPACE=/github/workspace" in call["args"]
