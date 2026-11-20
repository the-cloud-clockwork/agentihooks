import json
import subprocess
from pathlib import Path

import pytest
from mutmut.mutation.pragma_handling import PragmaParseError

from scripts.ci_lab import digests, expand_mutation, store
from scripts.ci_lab.objects import LocalObjects
from scripts.ci_lab.units import Unit
from scripts.ci_mutation.plan import MUTANT_SECONDS
from scripts.ci_mutation.selection import selected_mutant_names
from scripts.ci_mutation.stats_cache import collect_cached_stats
from scripts.ci_mutation.stats_dependencies import digest  # pyright: ignore[reportUnknownVariableType]

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
    units = expand_mutation.mutant_units(root, EVERY_LINE, covering or {}, {})
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
    units = expand_mutation.mutant_units(root, {PATH: lines}, {}, {})
    assert [function_of(unit) for unit in units] == [f"scripts.sample.{name}" for name in functions]


def test_a_unit_names_every_mutant_of_its_function_as_mutmut_does(tmp_path: Path) -> None:
    root = repository(tmp_path, MUTANT_FILES)
    units = expand_mutation.mutant_units(root, {PATH: [12, 28]}, {}, {})
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
        expand_mutation.mutant_units(root, {"scripts/broken.py": [3]}, {}, {})
    assert str(refused.value) == "# pragma: no mutate end at scripts/broken.py:1 without a # pragma: no mutate start"


def test_mutant_names_and_keys_are_stable_across_runs_and_checkouts(tmp_path: Path) -> None:
    first = expand_mutation.mutant_units(repository(tmp_path / "a", MUTANT_FILES), EVERY_LINE, {}, {})
    again = expand_mutation.mutant_units(tmp_path / "a", EVERY_LINE, {}, {})
    other = expand_mutation.mutant_units(repository(tmp_path / "b", MUTANT_FILES), EVERY_LINE, {}, {})
    assert first == again == other
    assert first
    assert len({unit.key for unit in first}) == len(first)


@pytest.mark.feature("lab-mutant-unit-split")
@pytest.mark.parametrize(
    ("timeout", "sizes"),
    [
        (1.0, [1, 1, 1, 1, 1]),
        (MUTANT_SECONDS + 1.0, [1, 1, 1, 1, 1]),
        (2 * MUTANT_SECONDS + 1.0, [1, 2, 2]),
        (3 * MUTANT_SECONDS - 1.0, [1, 2, 2]),
        (3 * MUTANT_SECONDS, [2, 3]),
    ],
)
def test_a_unit_priced_over_the_timeout_splits_into_the_fewest_parts_that_each_price_within_it(
    tmp_path: Path, timeout: float, sizes: list[int]
) -> None:
    root = repository(tmp_path, MUTANT_FILES)
    (whole,) = expand_mutation.mutant_units(root, {PATH: [12]}, {}, {})
    parts = expand_mutation.mutant_units(root, {PATH: [12]}, {}, {}, timeout=timeout)
    assert [len(unit.members) for unit in parts] == sizes
    assert [member for unit in parts for member in unit.members] == list(whole.members)
    assert [unit.kind for unit in parts] == ["mutant"] * len(parts)
    assert len({unit.key for unit in parts} | {whole.key}) == len(parts) + 1
    assert [unit.key for unit in parts] == [
        f"mutant-v{digests.SCHEMAS['mutant']}-{digest([whole.key.split('-', 2)[2], list(unit.members)])}"
        for unit in parts
    ]
    assert parts == expand_mutation.mutant_units(root, {PATH: [12]}, {}, {}, timeout=timeout)


@pytest.mark.feature("lab-mutant-unit-split")
def test_a_unit_priced_at_the_timeout_keeps_its_members_and_key(tmp_path: Path) -> None:
    root = repository(tmp_path, MUTANT_FILES)
    whole = expand_mutation.mutant_units(root, EVERY_LINE, {}, {})
    longest = max(len(unit.members) for unit in whole)
    assert expand_mutation.mutant_units(root, EVERY_LINE, {}, {}, timeout=longest * MUTANT_SECONDS) == whole


def test_the_same_function_in_another_module_gets_its_own_key(tmp_path: Path) -> None:
    root = repository(tmp_path, {**MUTANT_FILES, "scripts/copy.py": SAMPLE})
    units = expand_mutation.mutant_units(root, {PATH: [7], "scripts/copy.py": [7]}, {}, {})
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


@pytest.mark.feature("lab-mutant-unit-unlisted-tests")
def test_an_unlisted_test_module_enters_the_key_of_every_function(tmp_path: Path) -> None:
    root = repository(tmp_path, MUTANT_FILES)
    found = [
        {function_of(unit): unit.key for unit in expand_mutation.mutant_units(root, EVERY_LINE, {}, unlisted)}
        for unlisted in ({}, {"tests/test_new.py": "one"}, {"tests/test_new.py": "two"})
    ]
    assert found[0]
    assert found[0].keys() == found[1].keys() == found[2].keys()
    assert all(len({keys[name] for keys in found}) == 3 for name in found[0])


@pytest.mark.feature("lab-scheduler-mutation-units")
def test_covering_maps_each_function_of_the_changed_files_to_the_tests_that_ran_it_or_a_function_nested_in_it(
    tmp_path: Path,
) -> None:
    root = repository(tmp_path, MUTANT_FILES)
    manifest = {
        "units": [
            "scripts/sample.py::first",
            "scripts/sample.py::Box.grow",
            "scripts/sample.py::second.helper",
            "scripts/other.py::first",
            "scripts/sample.py::Box.shrink.inner",
        ],
        "tests": {
            "tests/test_a.py::test_one": {"units": [0, 3]},
            "tests/test_a.py::test_two@group": {"units": [1, 2]},
            "tests/test_b.py::test_three": {"units": [4, 0]},
            "tests/test_b.py::test_other": {"units": [3]},
        },
    }
    keys = {test: f"key {index}" for index, test in enumerate(manifest["tests"])}
    assert expand_mutation.covering(root, manifest, keys, {PATH: [7]}) == {
        "scripts.sample.x_first": {"tests/test_a.py::test_one": "key 0", "tests/test_b.py::test_three": "key 2"},
        "scripts.sample.xǁBoxǁgrow": {"tests/test_a.py::test_two@group": "key 1"},
        "scripts.sample.x_second": {"tests/test_a.py::test_two@group": "key 1"},
        "scripts.sample.xǁBoxǁshrink": {"tests/test_b.py::test_three": "key 2"},
    }
    assert expand_mutation.covering(root, manifest, keys, {}) == {}


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
    assert {unit.key for unit in units} == {f"stats-v{digests.SCHEMAS['stats']}-{key}" for key in stored}
    assert len(stored) == len(files)


@pytest.mark.feature("lab-mutation-verdict-key-prefix")
@pytest.mark.parametrize("schema_delta", [0, 1])
@pytest.mark.parametrize(
    ("kind", "timeout"),
    [("stats", 1800.0), ("mutant", 1800.0), ("mutant", 1.0)],
    ids=["stats", "whole-mutant", "split-mutant"],
)
def test_generated_mutation_unit_keys_can_be_stored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str, timeout: float, schema_delta: int
) -> None:
    import fakeredis

    monkeypatch.setitem(digests.SCHEMAS, kind, digests.SCHEMAS[kind] + schema_delta)
    root = repository(tmp_path / "repo", STATS_FILES if kind == "stats" else MUTANT_FILES)
    if kind == "stats":
        units = expand_mutation.stats_units(root, ["tests/test_a.py", "tests/test_b.py"])
    else:
        units = expand_mutation.mutant_units(root, {PATH: [12]}, {}, {}, timeout=timeout)
    verdicts = store.Store(fakeredis.FakeRedis(), LocalObjects(tmp_path / "objects"))
    assert units
    for unit in units:
        verdict = store.Verdict(unit.kind, unit.key, True, False, 1.0)
        assert verdicts.put(verdict, {"members": list(unit.members)}, b"completed")
        assert verdicts.get(unit.kind, unit.key) == verdict
        assert unit.key.startswith(f"{kind}-v{digests.SCHEMAS[kind]}-")
