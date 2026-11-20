import json
from pathlib import Path

import pytest


@pytest.mark.feature("kind-terminal-snapshot-base")
@pytest.mark.parametrize("source", ["terminal-worker.Dockerfile", "kind-smoke.sh"])
def test_terminal_packages_and_prefetch_use_the_locked_python(source: str) -> None:
    root = Path(__file__).parents[1]
    lock = json.loads((root / "docker/swarm-node/versions.lock").read_text())
    image = f"mirror.gcr.io/library/python:{lock['python']}-slim-bookworm"
    text = (root / "deploy/helm/agentihooks-swarm/ci" / source).read_text()
    assert image in text
    assert "python:3.12-slim-bookworm" not in text
