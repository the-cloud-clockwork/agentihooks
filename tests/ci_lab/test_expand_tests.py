import copy
import subprocess
from pathlib import Path
from typing import Any

import pytest

from scripts.ci_lab import expand_tests
from tests import dep_manifest

ALPHA = "def first():\n    return 1\n\n\ndef second():\n    return 2\n"
FILES = {
    "pyproject.toml": "[project]\nname = 'fixture'\n",
    "uv.lock": "version = 1\n",
    "scripts/alpha.py": ALPHA,
    "scripts/data.json": "{}\n",
    "hooks/beta.py": "def run():\n    return 3\n",
    "tests/conftest.py": "import pytest\n",
    "tests/helpers.py": "VALUE = 1\n",
    "tests/test_alpha.py": "def test_first():\n    pass\n\n\ndef test_second():\n    pass\n",
    "tests/test_beta.py": "def test_run():\n    pass\n\n\ndef test_spawn():\n    pass\n",
    "tests/test_group.py": "def test_one():\n    pass\n",
    "tests/fixtures/sample.txt": "sample\n",
    "docs/guide.md": "guide\n",
    "tool.py": "TOOL = 1\n",
    ".github/workflows/test.yml": "on: push\n",
}
UNITS = [
    "hooks/beta.py::<module>",
    "hooks/beta.py::run",
    "scripts/alpha.py::<module>",
    "scripts/alpha.py::first",
    "scripts/alpha.py::second",
]
MANIFEST: dict[str, Any] = {
    "version": 2,
    "commit": "base",
    "environment": "py312",
    "units": UNITS,
    "tests": {
        "tests/test_alpha.py::test_first": {"units": [2, 3], "files": [], "escapes": False},
        "tests/test_alpha.py::test_second": {"units": [2, 4], "files": [], "escapes": False},
        "tests/test_beta.py::test_run": {"units": [0, 1], "files": ["scripts/data.json"], "escapes": False},
        "tests/test_beta.py::test_spawn": {"units": [], "files": ["docs/guide.md"], "escapes": True},
        "tests/test_group.py::TestGroup::test_one@fakeredis": {"units": [0], "files": [], "escapes": False},
    },
    "lines": {},
}
EVERY_TEST = set(MANIFEST["tests"])
FIRST, SECOND, RUN, SPAWN, ONE = MANIFEST["tests"]


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=lab", "-c", "user.email=lab@example.invalid", "-c", "commit.gpgsign=false", *args],
        cwd=root,
        check=True,
        capture_output=True,
    )


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    for path, text in FILES.items():
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / path).write_text(text)
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "base")
    return tmp_path


def _tree(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and ".git" not in path.relative_to(root).parts
    }


def _plant(root: Path, path: str, content: bytes | None, manifest: dict[str, Any] = MANIFEST) -> set[str]:
    before = expand_tests.fingerprints(manifest, _tree(root))
    if content is None:
        (root / path).unlink()
    else:
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_bytes(content)
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "plant")
    after = expand_tests.fingerprints(manifest, _tree(root))
    assert set(before) == set(after) == set(manifest["tests"])
    return {test for test in before if before[test] != after[test]}


def test_function_body_change_rekeys_only_its_covering_tests(repository: Path) -> None:
    rekeyed = _plant(repository, "scripts/alpha.py", ALPHA.replace("return 1", "return 10").encode())
    assert rekeyed == {"tests/test_alpha.py::test_first", "tests/test_beta.py::test_spawn"}


@pytest.mark.parametrize("path", ["uv.lock", "pyproject.toml"])
def test_lockfile_change_rekeys_every_test(repository: Path, path: str) -> None:
    assert _plant(repository, path, b"changed\n") == EVERY_TEST


def test_lockfile_removal_rekeys_every_test(repository: Path) -> None:
    assert _plant(repository, "uv.lock", None) == EVERY_TEST


@pytest.mark.parametrize(
    ("path", "content", "expected"),
    [
        ("scripts/alpha.py", ALPHA.replace("return 1", "return 10").encode(), {FIRST, SPAWN}),
        ("scripts/alpha.py", ALPHA.replace("return 1", "return '\xff'").encode("latin-1"), {FIRST, SPAWN}),
        ("scripts/alpha.py", ALPHA + "\n\ndef third():\n    return 3\n", {SPAWN}),
        ("scripts/alpha.py", "LIMIT = 2\n\n\n" + ALPHA, {FIRST, SECOND, SPAWN}),
        ("scripts/data.json", '{"changed": true}\n', {RUN, SPAWN}),
        ("hooks/beta.py", None, {RUN, ONE, SPAWN}),
        ("hooks/gamma.py", "def other():\n    return 4\n", {SPAWN}),
        (
            "tests/test_alpha.py",
            FILES["tests/test_alpha.py"] + "\n\ndef test_third():\n    pass\n",
            {FIRST, SECOND, SPAWN},
        ),
        ("tests/test_alpha.py", "def broken(:\n", {FIRST, SECOND, SPAWN}),
        ("tests/test_group.py", None, {ONE, SPAWN}),
        ("tests/conftest.py", "import os\n", EVERY_TEST),
        ("tests/helpers.py", "VALUE = 2\n", EVERY_TEST),
        ("tests/fixtures/sample.txt", "changed\n", {SPAWN}),
        ("docs/guide.md", "changed\n", {SPAWN}),
        ("docs/notes.md", "notes\n", set()),
        ("tool.py", "TOOL = 2\n", EVERY_TEST),
        (".github/helper.py", "HELPER = 1\n", EVERY_TEST),
        ("uv.lock", "version = 2\n", EVERY_TEST),
    ],
)
def test_rekeyed_tests_match_the_manifest_invalidation(
    repository: Path, path: str, content: str | bytes | None, expected: set[str]
) -> None:
    planted = content.encode() if isinstance(content, str) else content
    rekeyed = _plant(repository, path, planted)
    assert rekeyed == expected
    assert dep_manifest.invalidated(MANIFEST, dep_manifest.changes(repository, "HEAD^", "HEAD"))[0] == expected


def test_a_change_to_an_imported_test_module_rekeys_the_importing_tests(repository: Path) -> None:
    (repository / "tests/helpers.py").write_text("from tests.test_alpha import test_first as reused\n")
    (repository / "tests/test_import.py").write_text("from . import helpers\n\n\ndef test_reuse():\n    pass\n")
    manifest = copy.deepcopy(MANIFEST)
    manifest["tests"]["tests/test_import.py::test_reuse"] = {"units": [], "files": [], "escapes": False}
    cycle = FILES["tests/test_alpha.py"] + "\n\nfrom tests.test_import import test_reuse\n"
    assert _plant(repository, "tests/test_alpha.py", cycle.encode(), manifest) == {
        FIRST,
        SECOND,
        SPAWN,
        "tests/test_import.py::test_reuse",
    }


def test_workflow_change_rekeys_nothing_while_the_invocation_rekeys_every_test(repository: Path) -> None:
    assert _plant(repository, ".github/workflows/test.yml", b"on: pull_request\n") == set()
    tree = _tree(repository)
    default = expand_tests.fingerprints(MANIFEST, tree)
    other = expand_tests.fingerprints(MANIFEST, tree, (*expand_tests.PYTEST_INVOCATION, "-x"))
    assert {test for test in default if default[test] != other[test]} == EVERY_TEST


def test_a_test_that_opened_files_outside_the_manifest_is_keyed_on_the_measured_tree(repository: Path) -> None:
    manifest = copy.deepcopy(MANIFEST)
    manifest["tests"]["tests/test_alpha.py::test_second"]["outside"] = ["/var/cache/sample"]
    assert _plant(repository, "scripts/data.json", b"[]\n", manifest) == {
        "tests/test_alpha.py::test_second",
        "tests/test_beta.py::test_run",
        "tests/test_beta.py::test_spawn",
    }


@pytest.mark.feature("packed-manifest-outside-files")
def test_a_packed_then_unpacked_record_with_outside_files_stays_keyed_on_the_measured_tree(repository: Path) -> None:
    manifest = copy.deepcopy(MANIFEST)
    manifest["tests"][SECOND]["outside"] = ["docs/guide.md"]
    unpacked = dep_manifest.unpack(dep_manifest.pack(copy.deepcopy(manifest)))
    assert unpacked == manifest
    assert _plant(repository, "scripts/data.json", b"[]\n", unpacked) == {SECOND, RUN, SPAWN}


@pytest.mark.parametrize(
    ("record", "expected"),
    [
        ({"units": [], "files": [], "escapes": False}, False),
        ({"units": [], "files": []}, True),
        ({"units": [], "files": [], "escapes": False, "outside": []}, False),
        ({"units": [], "files": [], "escapes": True}, True),
        ({"units": [], "files": [], "escapes": False, "outside": ["/etc/hosts"]}, True),
    ],
)
def test_broad_reads_the_tracer_signals(record: dict[str, Any], expected: bool) -> None:
    assert expand_tests.broad(record) is expected


def test_fingerprints_are_golden() -> None:
    tree = {
        "pyproject.toml": b"[project]\n",
        "scripts/alpha.py": ALPHA.encode(),
        "tests/test_alpha.py": FILES["tests/test_alpha.py"].encode(),
    }
    manifest = {
        "units": ["scripts/alpha.py::<module>", "scripts/alpha.py::first"],
        "tests": {
            "tests/test_alpha.py::test_first": {"units": [0, 1], "files": ["scripts/data.json"], "escapes": False},
            "tests/test_alpha.py::test_second": {"units": [], "files": [], "escapes": True},
        },
    }
    assert expand_tests.fingerprints(manifest, tree) == {
        "tests/test_alpha.py::test_first": "50453d70f855a2c09ef48a172dee6f277fb53e9d727059b5eb3ff0ebfca4ea01",
        "tests/test_alpha.py::test_second": "9107747a4089126a323cbfc2a542aa9a339ad2254648c5c96fa651fb32e1a19f",
    }


def _manifest(*tests: str) -> dict[str, Any]:
    return {"tests": {test: {"units": [], "files": [], "escapes": False} for test in tests}}


def test_each_file_under_the_budget_is_one_batch() -> None:
    manifest = _manifest("tests/test_b.py::TestB::test_two", "tests/test_a.py::test_one", "tests/test_b.py::test_one")
    durations = {
        "tests/test_a.py::test_one": 3.0,
        "tests/test_b.py::test_one": 4.0,
        "tests/test_b.py::TestB::test_two": 6.0,
    }
    assert expand_tests.batches(manifest, durations, 10.0) == [
        ("tests/test_a.py::test_one",),
        ("tests/test_b.py::TestB::test_two", "tests/test_b.py::test_one"),
    ]


@pytest.mark.parametrize(
    ("seconds", "target", "expected"),
    [
        ([5.0, 5.0, 5.0, 5.0], 10.0, [("a", "c"), ("b", "d")]),
        ([6.0, 6.0, 6.0, 1.0], 10.0, [("a", "d"), ("b",), ("c",)]),
        ([3.0, 3.0, 2.0, 2.0, 2.0], 6.0, [("a", "e"), ("b",), ("c", "d")]),
        ([30.0, 30.0, 1.0], 10.0, [("a",), ("b",), ("c",)]),
        ([1.0, 5.0, 5.0], 5.0, [("b",), ("c",), ("a",)]),
    ],
)
def test_an_over_budget_file_splits_into_the_fewest_balanced_parts(
    seconds: list[float], target: float, expected: list[tuple[str, ...]]
) -> None:
    names = [f"tests/test_slow.py::test_{letter}" for letter in "abcde"[: len(seconds)]]
    durations = dict(zip(names, seconds, strict=True))
    found = expand_tests.batches(_manifest(*names), durations, target)
    assert [tuple(name[-1] for name in batch) for batch in found] == expected


def test_a_file_at_the_budget_stays_whole() -> None:
    names = ["tests/test_even.py::test_a", "tests/test_even.py::test_b"]
    assert expand_tests.batches(_manifest(*names), dict.fromkeys(names, 5.0), 10.0) == [tuple(names)]


def test_a_grouped_file_stays_whole_over_the_budget() -> None:
    names = ["tests/test_group.py::test_a@fakeredis", "tests/test_group.py::test_b@fakeredis"]
    durations = {"tests/test_group.py::test_a": 8.0, "tests/test_group.py::test_b": 8.0}
    assert expand_tests.batches(_manifest(*names), durations, 10.0) == [tuple(names)]


def test_a_test_without_a_duration_counts_zero_seconds() -> None:
    names = ["tests/test_new.py::test_known", "tests/test_new.py::test_unknown"]
    durations = {"tests/test_new.py::test_known": 5.0}
    assert expand_tests.batches(_manifest(*names), durations, 5.5) == [tuple(names)]
