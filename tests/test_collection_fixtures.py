import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("order", [("a", "root", "b"), ("root", "a", "b"), ("a", "b", "root")])
@pytest.mark.parametrize("disable_autoload", ["", "1"])
def test_grouped_files_keep_directory_fixtures(tmp_path, order, disable_autoload):
    package = tmp_path / "checks"
    nested = package / "swarm"
    nested.mkdir(parents=True)
    (package / "__init__.py").touch()
    (nested / "__init__.py").touch()
    (package / "conftest.py").write_text("from tests.conftest import pytest_addoption, pytest_configure\n")
    (nested / "conftest.py").write_text(
        "import pytest\n"
        "@pytest.fixture(autouse=True)\n"
        "def classify(monkeypatch):\n"
        "    monkeypatch.setenv('GROUPED_CLASSIFIER', 'engineer')\n"
        "@pytest.fixture\n"
        "def scratch(tmp_path):\n"
        "    return tmp_path\n"
    )
    for name in ("a", "b"):
        (nested / f"test_{name}.py").write_text(
            "import os\n"
            "def test_scoped(scratch):\n"
            "    assert scratch.is_dir()\n"
            "    assert os.environ['GROUPED_CLASSIFIER'] == 'engineer'\n"
        )
    (package / "test_root.py").write_text(
        "import os\n"
        "def test_root(request):\n"
        "    assert 'classify' not in request.fixturenames\n"
        "    assert 'GROUPED_CLASSIFIER' not in os.environ\n"
    )
    files = {"root": package / "test_root.py", "a": nested / "test_a.py", "b": nested / "test_b.py"}
    environ = dict(
        os.environ,
        PYTHONPATH=str(Path(__file__).resolve().parents[1]),
        PYTEST_DISABLE_PLUGIN_AUTOLOAD=disable_autoload,
    )
    environ.pop("PYTEST_ADDOPTS", None)
    environ.pop("PYTEST_DISABLE_PLUGIN_AUTOLOAD", None)
    environ.pop("GROUPED_CLASSIFIER", None)
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", *(str(files[name]) for name in order)],
        cwd=tmp_path,
        env=environ,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "3 passed" in result.stdout
