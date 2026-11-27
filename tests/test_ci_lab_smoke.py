"""Both image smokes import every module the CI lab package walk finds, through one builder."""

import importlib
import shlex
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from types import ModuleType

import pytest
import yaml

import scripts.ci_lab
from scripts import ci_lab_smoke

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]
SWARM_IMAGE = "local/agentihooks-swarm:clients-check"
SWARM_STEP = "Import the lab scheduler's modules inside the swarm image"
LAB_PYTHON = "/opt/agentihooks-venv/bin/python"
LAB_EXTRAS = ["pytest", "xdist", "redis", "nats", "yaml", "boto3"]
PLANTED = f"{scripts.ci_lab.__name__}.planted"


def _swarm_argv() -> list[str]:
    """Return the arguments the swarm image smoke step hands the image's Python."""
    workflow = yaml.safe_load((ROOT / ".github/workflows/swarm-smoke.yml").read_text())
    (step,) = [step for step in workflow["jobs"]["smoke"]["steps"] if step.get("name") == SWARM_STEP]
    argv = shlex.split(step["run"])
    assert argv[:8] == ["docker", "run", "--rm", "--network", "none", "--entrypoint", "python", SWARM_IMAGE]
    return argv[8:]


def _lab_argv() -> list[str]:
    """Return the arguments the CI lab image smoke hands the venv Python among its image checks."""
    checks = (ROOT / "docker/ci-lab/smoke.sh").read_text().split("<<'CHECKS'\n")[1].split("\nCHECKS\n")[0]
    (line,) = [line for line in checks.splitlines() if line.startswith(f"{LAB_PYTHON} -m ")]
    return shlex.split(line)[1:]


CALL_SITES: dict[str, tuple[Callable[[], list[str]], list[str]]] = {
    "swarm": (_swarm_argv, ["scripts.ci_test_plan"]),
    "lab": (_lab_argv, LAB_EXTRAS),
}


@pytest.fixture
def plant(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Callable[[str, str], str]]:
    """Plant modules in the CI lab package from a folder searched before the package's own."""
    planted: list[str] = []
    monkeypatch.setattr(scripts.ci_lab, "__path__", [str(tmp_path), *scripts.ci_lab.__path__])

    def write(stem: str, source: str) -> str:
        (tmp_path / f"{stem}.py").write_text(source)
        importlib.invalidate_caches()
        planted.append(f"{scripts.ci_lab.__name__}.{stem}")
        return planted[-1]

    yield write
    for name in planted:
        sys.modules.pop(name, None)
        scripts.ci_lab.__dict__.pop(name.rpartition(".")[2], None)


@pytest.fixture
def imported(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record every name the builder imports; planted modules run, every other name stands in as an empty module."""
    names: list[str] = []
    real = importlib.import_module

    def load(name: str, package: str | None = None) -> ModuleType:
        names.append(name)
        return real(name, package) if name.startswith(PLANTED) else ModuleType(name)

    monkeypatch.setattr(importlib, "import_module", load)
    return names


@pytest.mark.feature("lab-image-smoke-imports")
def test_the_walk_lists_a_module_planted_in_the_lab_package(plant: Callable[[str, str], str]) -> None:
    name = plant("planted_probe", "VALUE = 1\n")
    modules = ci_lab_smoke.lab_modules()
    assert name in modules
    assert modules == sorted(modules)
    assert {
        "scripts.ci_lab.__main__",
        "scripts.ci_lab.contract",
        "scripts.ci_lab.executors",
        "scripts.ci_lab.remote",
        "scripts.ci_lab.remote.checkout",
    } <= set(modules)


@pytest.mark.feature("lab-image-smoke-imports")
@pytest.mark.parametrize("site", list(CALL_SITES))
def test_both_image_smokes_import_a_module_planted_in_the_lab_package(
    site: str,
    plant: Callable[[str, str], str],
    imported: list[str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    read, extras = CALL_SITES[site]
    argv = read()
    assert argv == ["-m", "scripts.ci_lab_smoke", *extras]
    name = plant("planted_probe", "VALUE = 1\n")
    assert ci_lab_smoke.main(argv[2:]) == 0
    assert sys.modules[name].VALUE == 1
    assert imported == [*ci_lab_smoke.lab_modules(), *extras]
    assert capsys.readouterr() == (f"imported: {', '.join(imported)}\n", "")


@pytest.mark.feature("lab-image-smoke-imports")
def test_the_builder_names_the_first_module_that_does_not_import_and_stops(
    plant: Callable[[str, str], str], imported: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    broken = plant("planted_broken", "import a_dependency_no_image_installs\n")
    modules = ci_lab_smoke.lab_modules()
    assert ci_lab_smoke.main(["json"]) == 1
    assert imported == modules[: modules.index(broken) + 1]
    assert capsys.readouterr() == (
        "",
        f"{broken} does not import: ModuleNotFoundError: No module named 'a_dependency_no_image_installs'\n",
    )


@pytest.mark.feature("lab-image-smoke-imports")
def test_the_builder_reads_its_extra_names_from_the_command_line(
    monkeypatch: pytest.MonkeyPatch, imported: list[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["scripts.ci_lab_smoke", "json", "csv"])
    assert ci_lab_smoke.main() == 0
    assert imported == [*ci_lab_smoke.lab_modules(), "json", "csv"]
