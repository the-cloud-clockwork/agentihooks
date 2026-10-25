import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest


@pytest.mark.parametrize("failure", ["collection", "fixture"])
def test_inherited_redis_credentials_never_reach_failure_output(tmp_path, failure):
    root = Path(__file__).resolve().parents[1]
    conftest = root / "tests" / "conftest.py"
    if conftest.exists():
        shutil.copyfile(conftest, tmp_path / "conftest.py")
    canary = uuid.uuid4().hex
    inherited = {
        "REDIS_URL": f"redis://test:{canary}@localhost:6379/0",
        "AGENTIHOOKS_SWARM_REDIS_URL": f"redis://test:{canary}@localhost:6379/0",
        "AGENTIHOOKS_HIVE_REDIS_URL": f"rediss://test:{canary}@localhost:6379/0",
        "REDIS_PASSWORD": canary,
        "REDISCLI_AUTH": canary,
        "SERVICE_REDIS_CONNECTION_STRING": canary,
        "redis_password": canary,
    }
    source = (
        "import os\nimport pytest\nobserved = {name: os.environ.get(name) for name in " + repr(tuple(inherited)) + "}\n"
    )
    if failure == "collection":
        source += "raise RuntimeError(f'collection failure: {observed}')\n"
    else:
        source += (
            "@pytest.fixture\ndef failing():\n"
            "    raise RuntimeError(f'fixture failure: {observed}')\n"
            "def test_failure(failing):\n    pass\n"
        )
    (tmp_path / "test_probe.py").write_text(source)
    env = {name: value for name, value in os.environ.items() if "REDIS" not in name.upper()}
    env.update(inherited)
    env.update(
        HOME=str(tmp_path / "home"),
        PYTHONPATH=os.pathsep.join((str(root), str(root / "scripts"))),
        PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
        PYTEST_ADDOPTS="",
    )
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--showlocals", "--confcutdir", str(tmp_path), str(tmp_path)],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    output = result.stdout + result.stderr
    assert result.returncode == (2 if failure == "collection" else 1), output
    assert f"{failure} failure:" in output, output
    assert canary not in output, output
