import copy
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from scripts import dep_manifest
from scripts.ci_lab import digests, executors, expand_tests, scheduler
from scripts.ci_lab.digests import Toolchain
from scripts.ci_lab.objects import LocalObjects, manifest_folder
from scripts.ci_lab.units import Unit

ALPHA = "def first():\n    return 1\n\n\ndef second():\n    return 2\n"
FILES = {
    "pyproject.toml": "[project]\nname = 'fixture'\n",
    ".github/test-requirements.lock": "version = 1\n",
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


@pytest.mark.feature("tracked-test-lock-cache-keys")
@pytest.mark.parametrize("path", [".github/test-requirements.lock", "pyproject.toml"])
def test_lockfile_change_rekeys_every_test(repository: Path, path: str) -> None:
    assert _plant(repository, path, b"changed\n") == EVERY_TEST


@pytest.mark.feature("tracked-test-lock-cache-keys")
def test_lockfile_removal_rekeys_every_test(repository: Path) -> None:
    assert _plant(repository, ".github/test-requirements.lock", None) == EVERY_TEST


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
        (".github/test-requirements.lock", "version = 2\n", EVERY_TEST),
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
        "tests/test_alpha.py::test_first": "ed5f3ba8a40286607a29357d071cc0a880c91abd882b321d70b64b723311c74f",
        "tests/test_alpha.py::test_second": "f0b37256e2d223dbdeb48a0dc09112641b9ceeebe91963ffcc955a652f3131ee",
    }


@pytest.mark.feature("lab-mutant-unit-unlisted-tests")
def test_unlisted_hashes_each_test_module_that_holds_no_manifest_test_with_its_conftests_and_imports() -> None:
    added = {
        "tests/test_new.py": b"from tests.helpers import VALUE\n\n\ndef test_new():\n    assert VALUE\n",
        "tests/inner/test_deep.py": b"",
        "tests/inner/conftest.py": b"",
        "conftest.py": b"ROOT = 1\n",
    }
    others = {
        "tests/test_data.json": b"{}\n",
        "scripts/test_tool.py": b"TOOL = 1\n",
        "tests/unused.py": b"X = 1\n",
        "tests/other/conftest.py": b"",
    }
    tree = {path: text.encode() for path, text in FILES.items()} | added | others
    outside = (
        "pyproject.toml",
        ".github/test-requirements.lock",
        "docs/guide.md",
        "tool.py",
        ".github/workflows/test.yml",
    )
    assert expand_tests.unlisted_test_hashes(MANIFEST, tree) == {
        path: hashlib.sha256(text).hexdigest() for path, text in tree.items() if path not in outside
    }


@pytest.mark.feature("lab-mutant-unit-unlisted-reads")
def test_unlisted_hashes_every_measured_and_test_tree_file_only_while_a_test_module_holds_no_manifest_test() -> None:
    tree = {path: text.encode() for path, text in FILES.items()}
    assert expand_tests.unlisted_test_hashes(MANIFEST, tree) == {}
    tree["tests/test_new.py"] = b"def test_new():\n    pass\n"
    trees = [
        "scripts/alpha.py",
        "scripts/data.json",
        "hooks/beta.py",
        "tests/conftest.py",
        "tests/helpers.py",
        "tests/test_alpha.py",
        "tests/test_beta.py",
        "tests/test_group.py",
        "tests/fixtures/sample.txt",
        "tests/test_new.py",
    ]
    assert expand_tests.unlisted_test_hashes(MANIFEST, tree) == {
        path: hashlib.sha256(tree[path]).hexdigest() for path in trees
    }


@pytest.mark.feature("lab-mutant-unit-unlisted-tests")
def test_unlisted_keys_every_test_module_the_mutant_runner_runs(repository: Path) -> None:
    extra = {"tests/inner/test_deep.py": "", "tests/test_data.json": "{}\n", "scripts/test_tool.py": "TOOL = 1\n"}
    for path, text in extra.items():
        (repository / path).parent.mkdir(parents=True, exist_ok=True)
        (repository / path).write_text(text)
    _git(repository, "add", "-A")
    _git(repository, "commit", "-qm", "extra")
    runs = executors._test_files(repository)  # pyright: ignore[reportPrivateUsage]
    assert set(runs) <= set(expand_tests.unlisted_test_hashes({"tests": {}}, _tree(repository)))
    assert "tests/inner/test_deep.py" in runs


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


@pytest.mark.feature("lab-scheduler-test-units")
def test_each_batch_is_one_test_unit_keyed_by_its_fingerprints_opened_files_and_toolchain(repository: Path) -> None:
    tree = _tree(repository)
    manifest = copy.deepcopy(MANIFEST)
    manifest["tests"][RUN]["outside"] = ["docs/guide.md", "gone.txt"]
    blobs = {path: f"blob of {path}" for path in tree}
    durations = {FIRST: 30.0, SECOND: 30.5, RUN: 30.0, SPAWN: 30.0}
    toolchain = Toolchain("3.12", {"ruff": "1"}, "lock", "project", "image", {"TZ": "UTC"})
    keys = expand_tests.fingerprints(manifest, tree)

    def batch(tests: tuple[str, ...], opened: dict[str, str]) -> Unit:
        found = {test: keys[test] for test in tests}
        inputs = digests.TestInputs(found, opened, expand_tests.PYTEST_INVOCATION, toolchain)
        return Unit("test", digests.test_key(inputs), tests)

    assert expand_tests.BATCH_SECONDS == 60.0
    assert expand_tests.units(manifest, tree, blobs, durations, toolchain) == (
        batch((SECOND,), {}),
        batch((FIRST,), {}),
        batch((RUN, SPAWN), {"docs/guide.md": "blob of docs/guide.md"}),
        batch((ONE,), {}),
    )


NEW = "tests/test_new.py"
UNLISTED = [NEW, *(f"tests/inner/test_{letter}.py" for letter in "hgfedcba")]
TOOLCHAIN = Toolchain("3.12", {"ruff": "1"}, "lock", "project", "image", {"TZ": "UTC"})


def _unlisted(root: Path) -> tuple[Unit, ...]:
    listed = [
        "tests/test_alpha.py",
        "tests/test_group.py",
        "tests/helpers.py",
        "tests/test_gone.py",
        "scripts/alpha.py",
        "docs/guide.md",
    ]
    return expand_tests.unlisted_units(MANIFEST, _tree(root), [*reversed(UNLISTED), NEW, *listed], TOOLCHAIN)


@pytest.mark.feature("lab-scheduler-test-units")
def test_unlisted_units_key_each_changed_test_module_the_manifest_lacks_as_a_broad_test(repository: Path) -> None:
    for path in UNLISTED:
        (repository / path).parent.mkdir(parents=True, exist_ok=True)
        (repository / path).write_text("def test_one():\n    pass\n")
    tree = _tree(repository)
    keys = expand_tests.fingerprints({"units": [], "tests": {path: {"files": []} for path in UNLISTED}}, tree)

    def unit(path: str) -> Unit:
        inputs = digests.TestInputs({path: keys[path]}, {}, expand_tests.PYTEST_INVOCATION, TOOLCHAIN)
        return Unit("test", digests.test_key(inputs), (path,))

    assert _unlisted(repository) == tuple(unit(path) for path in sorted(UNLISTED))


@pytest.mark.feature("lab-scheduler-test-units")
@pytest.mark.parametrize(
    ("path", "rekeyed"),
    [("scripts/data.json", True), ("tests/fixtures/sample.txt", True), ("tool.py", True), ("docs/guide.md", False)],
)
def test_an_unlisted_test_module_unit_rekeys_on_every_measured_and_shared_file(
    repository: Path, path: str, rekeyed: bool
) -> None:
    (repository / NEW).write_text("def test_new():\n    pass\n")
    before = _unlisted(repository)
    (repository / path).write_text("changed\n")
    after = _unlisted(repository)
    assert [unit.members for unit in after] == [(NEW,)]
    assert (before != after) is rekeyed


@pytest.mark.feature("lab-unlisted-module-outside-notes")
@pytest.mark.parametrize(
    ("path", "content", "rekeyed"),
    [
        ("docs/guide.md", b"changed\n", True),
        ("assets/reference.json", b"changed\n", True),
        ("assets/later.txt", b"added\n", True),
        ("docs/guide.md", None, True),
        ("docs/unnoted.md", b"changed\n", False),
        ("assets/other.json", b"changed\n", False),
    ],
    ids=["first-note", "second-note", "added", "removed", "unnoted", "other-module"],
)
def test_a_stored_note_rekeys_an_unlisted_module_only_on_its_outside_files(
    tmp_path: Path, path: str, content: bytes | None, rekeyed: bool
) -> None:
    repo = "/example/project"
    objects = LocalObjects(tmp_path / "objects")
    packed = dep_manifest.pack(copy.deepcopy(MANIFEST))
    objects.write_once(f"{manifest_folder('example/project')}base.json", json.dumps(packed).encode())
    scheduler.note_outside(objects, repo, "base", (NEW, FIRST), ["docs/guide.md"])
    scheduler.note_outside(
        objects, repo, "base", (NEW,), ["assets/reference.json", "assets/later.txt", "docs/guide.md"]
    )
    scheduler.note_outside(objects, repo, "base", ("tests/test_unrelated.py",), ["assets/other.json"])
    manifest = scheduler.StoredManifests(objects)(repo, ["base"])
    assert manifest is not None
    first = {**MANIFEST["tests"][FIRST], "outside": ["docs/guide.md"]}
    assert manifest["tests"] == {**MANIFEST["tests"], FIRST: first}
    tree = {name: text.encode() for name, text in FILES.items()}
    tree.update(
        {
            NEW: b"def test_new():\n    pass\n",
            "assets/reference.json": b"reference\n",
            "assets/other.json": b"other\n",
            "docs/unnoted.md": b"unnoted\n",
        }
    )
    before = expand_tests.unlisted_units(manifest, tree, [NEW], TOOLCHAIN)
    if content is None:
        del tree[path]
    else:
        tree[path] = content
    after = expand_tests.unlisted_units(manifest, tree, [NEW], TOOLCHAIN)
    assert [unit.members for unit in after] == [(NEW,)]
    assert (before != after) is rekeyed
