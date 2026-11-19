import importlib
import shlex
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import yaml


@pytest.mark.feature("swarm-image-lab-dependencies")
@pytest.mark.parametrize("missing", [None, "executors", "expand_mutation", "future_module"])
def test_swarm_image_smoke_imports_every_discovered_lab_module_and_refuses_a_missing_dependency(
    missing: str | None,
) -> None:
    root = Path(__file__).parents[1]
    workflow = yaml.safe_load((root / ".github/workflows/swarm-smoke.yml").read_text())
    step = next(
        step
        for step in workflow["jobs"]["smoke"]["steps"]
        if step.get("name") == "Import the lab scheduler's modules inside the swarm image"
    )
    argv = shlex.split(step["run"])
    assert argv[:7] == ["docker", "run", "--rm", "--network", "none", "--entrypoint", "python"]
    code = argv[argv.index("-c") + 1]
    names = ["scripts.ci_lab.executors", "scripts.ci_lab.expand_mutation", "scripts.ci_lab.future_module"]
    imported: list[str] = []

    real_import = importlib.import_module

    def load(name: str, package: str | None = None) -> object:
        if name not in names:
            return real_import(name, package)
        if name == f"scripts.ci_lab.{missing}":
            raise ModuleNotFoundError(name)
        imported.append(name)
        return None

    modules = [SimpleNamespace(name=name) for name in names]
    with patch("pkgutil.walk_packages", return_value=iter(modules)), patch("importlib.import_module", side_effect=load):
        if missing is not None:
            with pytest.raises(ModuleNotFoundError, match=missing):
                exec(code)
        else:
            exec(code)
            assert imported == sorted(names)
