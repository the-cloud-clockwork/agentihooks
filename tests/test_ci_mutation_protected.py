import ast
import importlib.machinery
import os
import py_compile
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

import scripts.ci_mutation.killers
from scripts.ci_mutation import protected, runner, selection

DRIVER = (
    "import os\n"
    "import sys\n"
    "from pathlib import Path\n"
    "from scripts.ci_mutation.killers import RECORD\n"
    "if __name__ == '__main__':\n"
    "    main = sys.modules['__main__']\n"
    "    entry = f'{main.__spec__.name} {main.__spec__.origin} {main.__loader__.name}'\n"
    "    Path(sys.argv[1]).write_text(f\"{RECORD} {os.environ.get('MUTATION_GRADER', 'unset')} {entry}\")\n"
)


@pytest.fixture
def isolated(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(sys, "meta_path", list(sys.meta_path))
    yield
    for name in [name for name in sys.modules if name.split(".")[0] == protected.PACKAGE]:
        sys.modules.pop(name)


def _grader(root: Path, record: str = "killed") -> Path:
    package = root / "scripts/ci_mutation"
    package.mkdir(parents=True)
    (root / "scripts/__init__.py").write_text("")
    (package / "__init__.py").write_text("")
    (package / "killers.py").write_text(f"RECORD = {record!r}\n")
    (package / "selection.py").write_text(DRIVER)
    return root


def _record(grader: Path) -> str:
    source = grader / "scripts/ci_mutation/selection.py"
    return f"killed unset {protected.DRIVER} {source} {protected.DRIVER}"


def _spec(finder: protected.ProtectedFinder, name: str) -> importlib.machinery.ModuleSpec:
    spec = finder.find_spec(name)
    assert spec is not None
    return spec


def _imports(source: str) -> str:
    return ast.unparse(protected.renamed(ast.parse(source)))


def test_scripts_imports_name_the_protected_package_and_others_stay() -> None:
    assert _imports("from scripts.ci_mutation.killers import KillMemory") == (
        "from mutation_grader.ci_mutation.killers import KillMemory"
    )
    assert _imports("from scripts import gh_config") == "from mutation_grader import gh_config"
    assert _imports("import scripts.ci_cpu_quota as quota") == "import mutation_grader.ci_cpu_quota as quota"
    assert _imports("from . import sibling") == "from . import sibling"
    assert _imports("from scriptsy import value\nimport os") == "from scriptsy import value\nimport os"


def test_a_bare_scripts_import_is_refused_since_it_would_bind_another_name() -> None:
    with pytest.raises(ImportError, match="import scripts.ci_cpu_quota needs an alias to load under mutation_grader"):
        protected.renamed(ast.parse("import scripts.ci_cpu_quota"))
    assert _imports("import json") == "import json"


def test_only_the_scripts_package_and_its_modules_are_renamed() -> None:
    assert protected.protected_name("scripts") == "mutation_grader"
    assert protected.protected_name("scripts.ci_mutation") == "mutation_grader.ci_mutation"
    assert protected.protected_name("scriptsy.ci") == "scriptsy.ci"
    assert protected.protected_name("hooks.scripts") == "hooks.scripts"


def test_the_finder_serves_only_the_protected_package_from_its_checkout(tmp_path: Path) -> None:
    finder = protected.ProtectedFinder(_grader(tmp_path))
    package = _spec(finder, "mutation_grader.ci_mutation")
    module = _spec(finder, "mutation_grader.ci_mutation.killers")
    assert package.origin == str(tmp_path / "scripts/ci_mutation/__init__.py")
    assert package.submodule_search_locations == [str(tmp_path / "scripts/ci_mutation")]
    assert isinstance(package.loader, protected.ProtectedLoader)
    assert module.origin == str(tmp_path / "scripts/ci_mutation/killers.py")
    assert module.submodule_search_locations is None
    assert isinstance(module.loader, protected.ProtectedLoader)
    assert _spec(finder, "mutation_grader").origin == str(tmp_path / "scripts/__init__.py")
    assert finder.find_spec("mutation_grader.ci_mutation.missing") is None
    assert finder.find_spec("scripts.ci_mutation.killers") is None
    assert finder.find_spec("mutation_graderx") is None


@pytest.mark.usefixtures("isolated")
def test_a_planted_head_driver_edit_cannot_change_the_kill_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    grader = _grader(tmp_path / "grader")
    py_compile.compile(str(grader / "scripts/ci_mutation/selection.py"))
    monkeypatch.setattr(scripts.ci_mutation.killers, "RECORD", "forged", raising=False)
    monkeypatch.setenv("MUTATION_GRADER", str(grader))
    record = tmp_path / "record"
    monkeypatch.setattr(sys, "argv", ["protected.py", str(record)])
    monkeypatch.setitem(sys.modules, "__main__", sys.modules["__main__"])
    protected.main(grader)
    assert record.read_text() == _record(grader)
    assert sys.argv == ["protected.py", str(record)]
    assert "MUTATION_GRADER" not in os.environ
    record.unlink()
    protected.main(grader)
    assert record.read_text() == _record(grader)
    assert sys.modules["mutation_grader.ci_mutation.killers"].__file__ == str(grader / "scripts/ci_mutation/killers.py")


def test_the_runner_command_ignores_a_planted_head_driver(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    head = _grader(tmp_path / "head", record="forged")
    grader = _grader(tmp_path / "grader")
    launcher = grader / "scripts/ci_mutation/protected.py"
    launcher.write_text(Path(protected.__file__).read_text())
    monkeypatch.setenv("MUTATION_GRADER", str(grader))
    record = tmp_path / "record"
    subprocess.run([*runner.driver_command(), str(record)], cwd=head, check=True)
    assert record.read_text() == _record(grader)


@pytest.mark.usefixtures("isolated")
def test_install_puts_the_protected_finder_first() -> None:
    root = Path(selection.__file__).resolve().parents[2]
    finder = protected.install(root)
    assert sys.meta_path[0] is finder
    assert finder.root == root


@pytest.mark.usefixtures("isolated")
def test_an_unchanged_driver_loaded_under_the_protected_package_counts_the_same_mutants() -> None:
    root = Path(selection.__file__).resolve().parents[2]
    protected.install(root)
    __import__(protected.DRIVER)
    driver = sys.modules[protected.DRIVER]
    source = "def total(values):\n    return sum(value + 1 for value in values if value > 0)\n"
    assert driver.mutation_counts("hooks/sample.py", source, {2}) == selection.mutation_counts(
        "hooks/sample.py", source, {2}
    )
    assert driver.LIGHT_BOOTSTRAP == selection.LIGHT_BOOTSTRAP.replace(
        "'scripts.ci_mutation.selection'", f"'{protected.DRIVER}'"
    )
    assert driver.KillMemory is sys.modules["mutation_grader.ci_mutation.killers"].KillMemory
    assert driver.KillMemory is not scripts.ci_mutation.killers.KillMemory


def test_the_runner_drives_selection_from_the_grader_only_when_it_carries_the_launcher(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    head = [sys.executable, "-m", "scripts.ci_mutation.selection"]
    monkeypatch.delenv("MUTATION_GRADER", raising=False)
    assert runner.driver_command() == head
    monkeypatch.setenv("MUTATION_GRADER", str(tmp_path))
    assert runner.driver_command() == head
    launcher = tmp_path / "scripts/ci_mutation/protected.py"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("")
    assert runner.driver_command() == [sys.executable, "-P", str(launcher)]
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MUTATION_GRADER", "")
    assert runner.driver_command() == head
    monkeypatch.setenv("MUTATION_GRADER", ".")
    assert runner.driver_command() == [sys.executable, "-P", str(launcher)]
