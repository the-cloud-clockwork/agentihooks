import importlib
import shlex
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import yaml

import scripts.ci_lab
from scripts import ci_lab_smoke


def _smoke_extras(root: Path) -> list[str]:
    """Return the extra module names the swarm image smoke step hands the lab smoke builder inside the image."""
    workflow = yaml.safe_load((root / ".github/workflows/swarm-smoke.yml").read_text())
    step = next(
        step
        for step in workflow["jobs"]["smoke"]["steps"]
        if step.get("name") == "Import the lab scheduler's modules inside the swarm image"
    )
    argv = shlex.split(step["run"])
    assert argv[:7] == ["docker", "run", "--rm", "--network", "none", "--entrypoint", "python"]
    assert argv[8:10] == ["-m", "scripts.ci_lab_smoke"]
    return argv[10:]


@pytest.mark.feature("swarm-image-lab-dependencies")
@pytest.mark.parametrize("missing", [None, "executors", "expand_mutation", "future_module"])
def test_swarm_image_smoke_imports_every_discovered_lab_module_and_refuses_a_missing_dependency(
    missing: str | None, capsys: pytest.CaptureFixture[str]
) -> None:
    extras = _smoke_extras(Path(__file__).parents[1])
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
        returned = ci_lab_smoke.main(extras)
    if missing is not None:
        assert returned == 1
        assert f"scripts.ci_lab.{missing} does not import" in capsys.readouterr().err
        assert imported == names[: names.index(f"scripts.ci_lab.{missing}")]
    else:
        assert returned == 0
        assert imported == sorted(names)


@pytest.mark.feature("swarm-image-lab-dependencies")
def test_swarm_image_smoke_reaches_every_lab_module_on_disk() -> None:
    lab = Path(scripts.ci_lab.__file__).parent
    expected = sorted(
        ".".join((scripts.ci_lab.__name__, *source.relative_to(lab).with_suffix("").parts)).removesuffix(".__init__")
        for source in lab.rglob("*.py")
        if source != lab / "__init__.py"
    )
    imported: list[str] = []
    real_import = importlib.import_module

    def load(name: str, package: str | None = None) -> object:
        if not name.startswith(f"{scripts.ci_lab.__name__}."):
            return real_import(name, package)
        imported.append(name)
        return None

    with patch("importlib.import_module", side_effect=load):
        assert ci_lab_smoke.main(_smoke_extras(Path(__file__).parents[1])) == 0
    assert {"scripts.ci_lab.executors", "scripts.ci_lab.expand_mutation"} <= set(imported)
    assert imported == expected
