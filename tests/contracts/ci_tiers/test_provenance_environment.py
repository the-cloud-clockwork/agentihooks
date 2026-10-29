import os
import subprocess
import zipfile
from importlib.metadata import distribution
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[3]


def test_provenance_installs_yaml_for_its_isolated_interpreter(tmp_path):
    package = distribution("PyYAML")
    tag = next(
        line.removeprefix("Tag: ") for line in package.read_text("WHEEL").splitlines() if line.startswith("Tag: ")
    )
    wheels = tmp_path / "wheels"
    wheels.mkdir()
    with zipfile.ZipFile(wheels / f"pyyaml-{package.version}-{tag}.whl", "w") as archive:
        for entry in package.files:
            if ".." not in entry.parts:
                archive.write(package.locate_file(entry), str(entry))
    steps = yaml.safe_load((ROOT / "scripts/ci_duration_provenance.yml").read_text())
    install = next(step for step in steps if step.get("name") == "Install provenance dependencies")
    env = dict(
        os.environ,
        RUNNER_TEMP=str(tmp_path),
        PIP_NO_INDEX="1",
        PIP_FIND_LINKS=str(wheels),
        PIP_DISABLE_PIP_VERSION_CHECK="1",
    )
    result = subprocess.run(
        ["bash", "-e", "-c", install["run"]],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    interpreter = tmp_path / "provenance-python/bin/python"
    assert interpreter.is_file()
    result = subprocess.run(
        [str(interpreter), "-I", "-c", "import yaml; print(yaml.__version__)"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == package.version
