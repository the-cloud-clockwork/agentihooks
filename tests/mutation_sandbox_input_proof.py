import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from scripts.ci_mutation.runner import prepare_workspace


@pytest.mark.parametrize("stage", ["source", "workspace", "mutants"])
def test_full_selection_reads_the_same_repository_inputs(tmp_path, stage):
    root = Path(__file__).resolve().parents[1]
    tests = sorted(path.relative_to(root).as_posix() for path in (root / "tests").rglob("test_*.py"))
    baseline = root
    if stage != "source":
        work = tmp_path / "workspace"
        work.mkdir()
        prepare_workspace(root, work, ["hooks/config.py"], tests)
        baseline = work
        if stage == "mutants":
            baseline = work / "mutants"
            baseline.mkdir()
            config = tomllib.loads((work / "pyproject.toml").read_text())["tool"]["mutmut"]
            for name in ["hooks", "scripts", "tests", "pyproject.toml", *config["also_copy"]]:
                source = work / name
                if source.is_dir():
                    shutil.copytree(source, baseline / name, dirs_exist_ok=True)
                elif source.is_file():
                    (baseline / name).parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, baseline / name)
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests", "-q", "-n", "2", "--dist", "loadgroup", "--tb=short"],
        cwd=baseline,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTEST_ADDOPTS": ""},
    )
    assert result.returncode == 0, result.stdout + result.stderr
