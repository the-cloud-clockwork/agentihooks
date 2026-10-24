import os
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    ("arguments", "route"),
    [
        (["python", "/opt/swarm-node/supervisor.py", "attempt", "launch"], "supervisor"),
        (["python", "/opt/swarm-node/worker_image.py", "report"], "tini"),
        (["python"], "tini"),
        (["cat", "/opt/swarm-node/supervisor.py"], "tini"),
    ],
)
def test_entrypoint_executes_supervisor_directly_and_keeps_tini_for_other_commands(tmp_path, arguments, route):
    root = Path(__file__).resolve().parents[1]
    tini = tmp_path / "tini"
    tini.write_text('#!/usr/bin/env bash\nprintf "tini\\n"\n')
    tini.chmod(0o700)
    interpreter = tmp_path / "python"
    interpreter.write_text('#!/usr/bin/env bash\nprintf "supervisor\\n"\n')
    interpreter.chmod(0o700)
    entrypoint = tmp_path / "entrypoint.sh"
    entrypoint.write_text(
        (root / "docker/swarm-node/entrypoint.sh")
        .read_text()
        .replace("/opt/swarm-node/ssh/start.sh", "true")
        .replace("/usr/bin/tini", str(tini))
    )
    result = subprocess.run(
        ["bash", str(entrypoint), *arguments],
        env={**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"},
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == route
