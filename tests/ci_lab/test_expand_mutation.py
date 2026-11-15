import json
import subprocess
from pathlib import Path

import pytest
from mutmut.mutation.pragma_handling import PragmaParseError

from scripts.ci_lab import expand_mutation
from scripts.ci_lab.units import Unit
from scripts.ci_mutation.selection import selected_mutant_names
from scripts.ci_mutation.stats_cache import collect_cached_stats

SAMPLE = """import functools

LIMIT = 1


def first(value):
    return value + LIMIT


def second(value):
    doubled = value * 2
    return doubled + 1


def quiet():
    pass


@functools.cache
def third(value):
    return value % 3


class Box:
    SIZE = 3

    def grow(self, value):
        return value + self.SIZE

    def shrink(self, value):
        return value - 1


if LIMIT:

    def fallback(value):
        return value + 1
"""
PATH = "scripts/sample.py"
EVERY_LINE = {PATH: range(1, 40)}
MUTANT_FILES = {
    "pyproject.toml": '[project]\nname = "fixture"\n\n[project.optional-dependencies]\ndev = ["mutmut==3.6.0"]\n',
    "scripts/__init__.py": "",
    "scripts/other.py": "OTHER = 1\n",
    "scripts/sample.py": SAMPLE,
    "scripts/ci_mutation/__init__.py": "",
    "scripts/ci_mutation/tool.py": "TOOL = 1\n",
}
TEST_A = """from pathlib import Path

from scripts.sample import sample

DATA = "fixtures/data.json"
DURATIONS = ".test_durations"
GONE = "fixtures/gone.json"


def test_sample():
    assert sample() == 1
    assert sorted(Path("tests/fixtures").glob("*.json"))
"""
STATS_FILES = {
    ".test_durations": "{}\n",
    "pyproject.toml": '[project]\nname = "fixture"\n',
    "setup.cfg": "[tool:pytest]\n",
    "scripts/__init__.py": "",
    "scripts/sample.py": "def sample():\n    return 1\n",
    "scripts/ci_mutation/__init__.py": "",
    "scripts/ci_mutation/tool.py": "TOOL = 1\n",
    "tests/__init__.py": "",
    "tests/elsewhere.json": "{}\n",
    "tests/fixtures/data.json": "{}\n",
    "tests/fixtures/gone.json": "{}\n",
    "tests/test_a.py": TEST_A,
    "tests/test_b.py": "def test_alone():\n    assert True\n",
}


def repository(root: Path, files: dict[str, str]) -> Path:
    """Write files under root and track them all in a fresh git repository."""
    for name, text in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    return root


def function_of(unit: Unit) -> str:
    """Return the qualified mutmut function name a mutant unit covers."""
    return unit.members[0].rpartition("__mutmut_")[0]


def keys(root: Path, covering: dict[str, dict[str, str]] | None = None) -> dict[str, str]:
    """Map each function of the sample module that has mutants to its unit key."""
    units = expand_mutation.mutant_units(root, EVERY_LINE, covering or {})
    return {function_of(unit): unit.key for unit in units}


def edit(root: Path, name: str, old: str, new: str) -> None:
    """Replace old with new in one fixture file, failing when old is absent."""
    target = root / name
    text = target.read_text()
    assert old in text
    target.write_text(text.replace(old, new))


@pytest.mark.parametrize(
    ("lines", "functions"),
    [
        ([7], ["x_first"]),
        ([8], ["x_second"]),
        ([12], ["x_second"]),
        ([16], []),
        ([19], ["x_third"]),
        ([25], []),
        ([28, 31], ["xǁBoxǁgrow", "xǁBoxǁshrink"]),
        ([37], []),
    ],
)
def test_one_unit_per_changed_function_with_mutants(tmp_path: Path, lines: list[int], functions: list[str]) -> None:
    root = repository(tmp_path, MUTANT_FILES)
    units = expand_mutation.mutant_units(root, {PATH: lines}, {})
    assert [function_of(unit) for unit in units] == [f"scripts.sample.{name}" for name in functions]


def test_a_unit_names_every_mutant_of_its_function_as_mutmut_does(tmp_path: Path) -> None:
    root = repository(tmp_path, MUTANT_FILES)
    units = expand_mutation.mutant_units(root, {PATH: [12, 28]}, {})
    owners = {"scripts.sample.x_second", "scripts.sample.xǁBoxǁgrow"}
    every = [f"scripts.sample.{name}" for name in selected_mutant_names(PATH, SAMPLE, set(range(1, 40)))]
    assert [member for unit in units for member in unit.members] == [
        name for name in every if name.rpartition("__mutmut_")[0] in owners
    ]
    assert [unit.kind for unit in units] == ["mutant", "mutant"]


def test_a_malformed_pragma_is_refused_naming_its_file(tmp_path: Path) -> None:
    broken = "# pragma: no mutate end\ndef first(value):\n    return value + 1\n"
    root = repository(tmp_path, {**MUTANT_FILES, "scripts/broken.py": broken})
    with pytest.raises(PragmaParseError) as refused:
        expand_mutation.mutant_units(root, {"scripts/broken.py": [3]}, {})
    assert str(refused.value) == "# pragma: no mutate end at scripts/broken.py:1 without a # pragma: no mutate start"


def test_mutant_names_and_keys_are_stable_across_runs_and_checkouts(tmp_path: Path) -> None:
    first = expand_mutation.mutant_units(repository(tmp_path / "a", MUTANT_FILES), EVERY_LINE, {})
    again = expand_mutation.mutant_units(tmp_path / "a", EVERY_LINE, {})
    other = expand_mutation.mutant_units(repository(tmp_path / "b", MUTANT_FILES), EVERY_LINE, {})
    assert first == again == other
    assert first
    assert len({unit.key for unit in first}) == len(first)


def test_the_same_function_in_another_module_gets_its_own_key(tmp_path: Path) -> None:
    root = repository(tmp_path, {**MUTANT_FILES, "scripts/copy.py": SAMPLE})
    units = expand_mutation.mutant_units(root, {PATH: [7], "scripts/copy.py": [7]}, {})
    assert [function_of(unit) for unit in units] == ["scripts.copy.x_first", "scripts.sample.x_first"]
    assert units[0].key != units[1].key


def test_a_whitespace_only_edit_keeps_the_key_of_every_function_it_falls_outside(tmp_path: Path) -> None:
    root = repository(tmp_path, MUTANT_FILES)
    before = keys(root)
    edit(root, PATH, "LIMIT = 1\n\n\n", "LIMIT=1\n\n\n\n\n")
    edit(root, PATH, "doubled = value * 2", "doubled = value*2")
    edit(root, PATH, "    SIZE = 3\n", "    SIZE  =  3\n\n")
    after = keys(root)
    kept = {name for name in before if name != "scripts.sample.x_second"}
    assert {name for name in kept if after[name] == before[name]} == kept


def test_a_body_edit_changes_only_its_function_key(tmp_path: Path) -> None:
    root = repository(tmp_path, MUTANT_FILES)
    before = keys(root)
    edit(root, PATH, "return value + LIMIT", "return value + LIMIT + 1")
    edit(root, PATH, "return value - 1", "return value - 2")
    after = keys(root)
    assert after.keys() == before.keys()
    assert {name for name in before if after[name] != before[name]} == {
        "scripts.sample.x_first",
        "scripts.sample.xǁBoxǁshrink",
    }


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("LIMIT = 1", "LIMIT = 2"),
        ("SIZE = 3", "SIZE = 4"),
        ("        return value + 1", "        return value + 2"),
    ],
)
def test_an_edit_to_code_no_trampolined_function_holds_changes_every_key(tmp_path: Path, old: str, new: str) -> None:
    root = repository(tmp_path, MUTANT_FILES)
    before = keys(root)
    edit(root, PATH, old, new)
    after = keys(root)
    assert after.keys() == before.keys()
    assert all(after[name] != before[name] for name in before)


@pytest.mark.parametrize(
    ("name", "old", "new", "changed"),
    [
        ("pyproject.toml", "mutmut==3.6.0", "mutmut==3.6.1", True),
        ("pyproject.toml", 'name = "fixture"', 'name = "renamed"', False),
        ("scripts/ci_mutation/tool.py", "TOOL = 1", "TOOL = 2", True),
        ("scripts/other.py", "OTHER = 1", "OTHER = 2", False),
    ],
)
def test_only_the_mutmut_pin_and_runner_tree_enter_the_key(
    tmp_path: Path, name: str, old: str, new: str, changed: bool
) -> None:
    root = repository(tmp_path, MUTANT_FILES)
    before = keys(root)
    edit(root, name, old, new)
    after = keys(root)
    assert after.keys() == before.keys()
    assert [after[function] != before[function] for function in before] == [changed] * len(before)


@pytest.mark.parametrize("line", ['dev = ["mutmut==3.6.0"]', "dev = ['mutmut == 3.6.0']"])
def test_the_mutmut_pin_is_read_from_pyproject(tmp_path: Path, line: str) -> None:
    (tmp_path / "pyproject.toml").write_text(f"[project.optional-dependencies]\n{line}\n")
    assert expand_mutation.mutmut_pin(tmp_path) == "3.6.0"


@pytest.mark.parametrize("line", ['dev = ["mutmut>=3.6"]', 'dev = ["pytest==8.0"]'])
def test_a_pyproject_that_pins_no_mutmut_version_is_refused(tmp_path: Path, line: str) -> None:
    (tmp_path / "pyproject.toml").write_text(f"[project.optional-dependencies]\n{line}\n")
    with pytest.raises(ValueError) as refused:
        expand_mutation.mutmut_pin(tmp_path)
    assert str(refused.value) == f"{tmp_path / 'pyproject.toml'} pins no mutmut version"


def test_covering_fingerprints_enter_only_their_function_key(tmp_path: Path) -> None:
    root = repository(tmp_path, MUTANT_FILES)
    name = "scripts.sample.x_first"
    base = keys(root)
    one = keys(root, {name: {"tests/test_a.py::test_first": "one"}})
    two = keys(root, {name: {"tests/test_a.py::test_first": "two"}})
    assert len({base[name], one[name], two[name]}) == 3
    assert all(one[other] == two[other] == base[other] for other in base if other != name)


def test_stats_units_carry_the_stats_cache_identity(tmp_path: Path) -> None:
    root = repository(tmp_path / "repo", STATS_FILES)
    for name in ("setup.cfg", "tests/fixtures/gone.json"):
        (root / name).unlink()
    files = ["tests/test_a.py", "tests/test_b.py"]
    cache = tmp_path / "cache"

    def collect(chosen: list[str]) -> list[dict]:
        return [{"status": 0, "tests": {}, "durations": {f"{path}::test": 1.0 for path in chosen}, "cpu": 1.0}]

    collect_cached_stats(root, cache, {}, files, ["-q"], collect)
    stored = {json.loads(record.read_text())["key"] for record in cache.glob("*.json")}
    units = expand_mutation.stats_units(root, files, ["-q"])
    assert [(unit.kind, unit.members) for unit in units] == [("stats", (path,)) for path in files]
    assert {unit.key for unit in units} == stored
    assert len(stored) == len(files)
