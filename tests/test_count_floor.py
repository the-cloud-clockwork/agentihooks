import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit
_ROOT = Path(__file__).resolve().parents[1]

SUITE = """
import pytest

def test_a():
    pass

@pytest.mark.parametrize("n", [1, 2])
def test_b(n):
    pass
"""
OTHER = """
def test_c():
    pass
"""


def _tree(root: Path, files: dict[str, str]) -> Path:
    for name, text in files.items():
        path = root / "tests" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return root


def _floor(tmp_path, base: dict[str, str], head: dict[str, str], sources: dict[str, dict[str, str]] | None = None):
    for side, files in (sources or {}).items():
        for name, text in files.items():
            path = tmp_path / side / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "tests.count_floor",
            "--base",
            str(_tree(tmp_path / "base", base)),
            "--head",
            str(_tree(tmp_path / "head", head)),
        ],
        cwd=_ROOT,
        capture_output=True,
        text=True,
    )


def _collection_tree(tmp_path):
    root = _tree(tmp_path / "tree", {"test_a.py": SUITE})
    (root / ".gitignore").write_text("__pycache__/\n.collection-runs\n")
    probe = root / "conftest.py"
    probe.write_text(
        "from pathlib import Path\n"
        "p = Path('.collection-runs')\n"
        "p.write_text(p.read_text() + 'x' if p.exists() else 'x')\n"
    )
    for arguments in (
        ["init", "-b", "fixture"],
        ["add", "tests", "conftest.py", ".gitignore"],
        ["-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-m", "Fixture"],
    ):
        subprocess.run(["git", *arguments], cwd=root, check=True, capture_output=True)
    return root


@pytest.mark.parametrize("lost", [False, True])
def test_the_floor_exports_the_same_head_collection_without_collecting_it_again(tmp_path, monkeypatch, lost):
    from tests.count_floor import main

    base = _collection_tree(tmp_path / "base")
    head = _collection_tree(tmp_path / "head")
    expected = ["tests/test_a.py::test_a", "tests/test_a.py::test_b[1]", "tests/test_a.py::test_b[2]"]
    if lost:
        (head / "tests" / "test_a.py").write_text("def test_a():\n    pass\n")
        expected = ["tests/test_a.py::test_a"]
    output = tmp_path / "collected.json"
    monkeypatch.setenv("TEST_COUNT_HEAD_COLLECTION", str(output))
    assert main(["--base", str(base), "--head", str(head)]) == int(lost)
    assert json.loads(output.read_text()) == expected
    assert (head / ".collection-runs").read_text() == "x"
    from tests.duration_coverage import collected_tests

    assert json.loads(output.read_text()) == collected_tests(head)


def test_a_failed_head_collection_exports_no_list_for_splitting(tmp_path, monkeypatch):
    from tests.count_floor import main

    base = _collection_tree(tmp_path / "base")
    head = _collection_tree(tmp_path / "head")
    (head / "tests" / "test_a.py").write_text("this is invalid python\n")
    output = tmp_path / "collected.json"
    monkeypatch.setenv("TEST_COUNT_HEAD_COLLECTION", str(output))
    assert main(["--base", str(base), "--head", str(head)]) == 1
    assert not output.exists()


def test_collection_reuses_only_an_exact_clean_tree_and_environment(tmp_path):
    from tests.count_floor import collect_all

    root = _collection_tree(tmp_path)
    cache = tmp_path / "cache"
    expected = {"head": ["tests/test_a.py::test_a", "tests/test_a.py::test_b[1]", "tests/test_a.py::test_b[2]"]}
    assert collect_all({"head": root}, "tests/", cache=cache, environment="first") == expected
    assert collect_all({"head": root}, "tests/", cache=cache, environment="first") == expected
    assert (root / ".collection-runs").read_text() == "x"
    assert collect_all({"head": root}, "tests/", cache=cache, environment="second") == expected
    assert (root / ".collection-runs").read_text() == "xx"
    (root / "tests" / "test_a.py").write_text(SUITE + OTHER)
    changed = collect_all({"head": root}, "tests/", cache=cache, environment="first")
    assert len(changed["head"]) == 4
    assert (root / ".collection-runs").read_text() == "xxx"


@pytest.mark.parametrize("record", ["{}", "[3]", "[]", "null"])
def test_an_invalid_cached_collection_cannot_grade_a_floor(tmp_path, record):
    from tests.count_floor import collect_all

    root = _collection_tree(tmp_path)
    cache = tmp_path / "cache"
    assert collect_all({"head": root}, "tests/", cache=cache, environment="first")["head"]
    next(cache.glob("*.json")).write_text(record)
    assert not collect_all({"head": root}, "tests/", cache=cache, environment="first")["head"]
    assert (root / ".collection-runs").read_text() == "x"


def test_a_failed_collection_is_never_cached(tmp_path):
    from tests.count_floor import collect_all

    root = _collection_tree(tmp_path)
    (root / "tests" / "test_a.py").write_text("raise RuntimeError('collection fails')\n")
    subprocess.run(["git", "add", "tests/test_a.py"], cwd=root, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-m", "Failure"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    cache = tmp_path / "cache"
    assert collect_all({"head": root}, "tests/", cache=cache, environment="first") == {"head": None}
    assert not cache.exists()


def test_committed_collection_changes_cannot_reuse_an_old_count(tmp_path):
    from tests.count_floor import collect_all

    root = _collection_tree(tmp_path)
    cache = tmp_path / "cache"
    assert len(collect_all({"head": root}, "tests/", cache=cache, environment="first")["head"]) == 3
    (root / "tests" / "test_a.py").write_text(OTHER)
    subprocess.run(["git", "add", "tests/test_a.py"], cwd=root, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-m", "Changed"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    assert collect_all({"head": root}, "tests/", cache=cache, environment="first") == {
        "head": ["tests/test_a.py::test_c"]
    }
    assert (root / ".collection-runs").read_text() == "xx"


def test_changed_pytest_selection_cannot_reuse_a_full_collection(tmp_path, monkeypatch):
    from tests.count_floor import collect_all

    root = _collection_tree(tmp_path)
    cache = tmp_path / "cache"
    monkeypatch.delenv("PYTEST_ADDOPTS", raising=False)
    assert len(collect_all({"head": root}, "tests/", cache=cache, environment="first")["head"]) == 3
    monkeypatch.setenv("PYTEST_ADDOPTS", "-k test_b")
    assert collect_all({"head": root}, "tests/", cache=cache, environment="first") == {
        "head": ["tests/test_a.py::test_b[1]", "tests/test_a.py::test_b[2]"]
    }
    assert (root / ".collection-runs").read_text() == "xx"


def test_collection_splits_across_processes_and_matches_one_process(tmp_path, monkeypatch):
    import tests.count_floor as count_floor

    root = _tree(
        tmp_path / "tree",
        {
            "test_a.py": SUITE,
            "alpha/test_x.py": OTHER,
            "helpers/__init__.py": "",
            "test_m.py": OTHER.replace("test_c", "test_m"),
            "zeta/inner/test_z.py": SUITE,
            "conftest.py": "import os\nwith open('pids', 'a') as out:\n    out.write(f'{os.getpid()}\\n')\n",
        },
    )
    single = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider", "tests/"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    )
    expected = [line for line in single.stdout.splitlines() if "::" in line]
    (root / "pids").unlink()
    monkeypatch.setattr(count_floor, "collection_processes", lambda: 3)
    assert count_floor.collect_all({"head": root}, "tests/") == {"head": expected}
    assert len(expected) == 8
    assert len(set((root / "pids").read_text().split())) == 3


def test_collection_groups_are_contiguous_runs_weighted_by_test_files(tmp_path, monkeypatch):
    from tests.count_floor import collection_groups

    root = (
        _tree(
            tmp_path,
            {
                "__init__.py": "",
                "conftest.py": "",
                "alpha/test_x.py": OTHER,
                "alpha/test_y.py": OTHER,
                "beta/data.txt": "",
                "notes.txt": "",
                "test_a.py": SUITE,
                "test_b.py": OTHER,
                "test_data.json": "",
            },
        )
        / "tests"
    )
    shared = ["__init__.py", "conftest.py"]
    assert collection_groups(root, 2) == [
        [*shared, "alpha", "beta", "notes.txt"],
        [*shared, "test_a.py", "test_b.py", "test_data.json"],
    ]
    assert collection_groups(root, 3) == [
        [*shared, "alpha", "beta", "notes.txt"],
        [*shared, "test_a.py"],
        [*shared, "test_b.py", "test_data.json"],
    ]
    assert collection_groups(root, 1) == [
        [*shared, "alpha", "beta", "notes.txt", "test_a.py", "test_b.py", "test_data.json"]
    ]
    assert collection_groups(tmp_path / "missing", 4) == [shared]
    listed = Path.iterdir
    monkeypatch.setattr(Path, "iterdir", lambda path: reversed(list(listed(path))))
    assert collection_groups(root, 2)[0] == [*shared, "alpha", "beta", "notes.txt"]


def test_each_collected_side_shares_the_processes(tmp_path, monkeypatch):
    import tests.count_floor as count_floor

    files = {
        "alpha/test_x.py": OTHER,
        "test_a.py": SUITE,
        "test_m.py": OTHER.replace("test_c", "test_m"),
        "zeta/test_z.py": SUITE,
        "conftest.py": "import os\nwith open('pids', 'a') as out:\n    out.write(f'{os.getpid()}\\n')\n",
    }
    trees = {side: _tree(tmp_path / side, files) for side in ("base", "head")}
    monkeypatch.setattr(count_floor, "collection_processes", lambda: 4)
    collected = count_floor.collect_all(trees, "tests/")
    assert len(collected["base"]) == len(collected["head"]) == 8
    assert [len(set((tree / "pids").read_text().split())) for tree in trees.values()] == [2, 2]


def test_one_failed_group_fails_the_whole_side(tmp_path, monkeypatch):
    import tests.count_floor as count_floor

    root = _tree(tmp_path, {"alpha/test_x.py": "raise RuntimeError('collection fails')\n", "test_a.py": SUITE})
    monkeypatch.setattr(count_floor, "collection_processes", lambda: 2)
    assert count_floor.collect_all({"head": root}, "tests/") == {"head": None}


def test_groups_that_overlap_fail_the_side_instead_of_inflating_the_count(tmp_path, monkeypatch):
    import tests.count_floor as count_floor

    root = _tree(tmp_path, {"test_a.py": SUITE, "test_b.py": OTHER})
    every = ["__init__.py", "conftest.py", "test_a.py", "test_b.py"]
    monkeypatch.setattr(count_floor, "collection_groups", lambda root, count: [every, every])
    assert count_floor.collect_all({"head": root}, "tests/") == {"head": None}


def test_a_group_that_collects_no_tests_adds_nothing(tmp_path, monkeypatch):
    import tests.count_floor as count_floor

    root = _tree(tmp_path, {"test_a.py": SUITE, "test_empty.py": "x = 1\n"})
    monkeypatch.setattr(count_floor, "collection_processes", lambda: 2)
    assert count_floor.collection_groups(root / "tests", 2)[1][-1] == "test_empty.py"
    assert count_floor.collect_all({"head": root}, "tests/") == {
        "head": ["tests/test_a.py::test_a", "tests/test_a.py::test_b[1]", "tests/test_a.py::test_b[2]"]
    }


def test_collection_processes_follow_the_cpu_quota(monkeypatch):
    import scripts.ci_cpu_quota as quota
    from tests.count_floor import collection_processes

    monkeypatch.setattr(os, "sched_getaffinity", lambda pid: set(range(6)))
    monkeypatch.setattr(quota, "quota_cpus", lambda: 2.5)
    assert collection_processes() == 2
    monkeypatch.setattr(quota, "quota_cpus", lambda: None)
    assert collection_processes() == 6


def _recorded_floor(tmp_path, base_files, head_files, recorded, environ=None):
    base = _collection_tree(tmp_path / "b")
    for name, text in base_files.items():
        (base / "tests" / name).write_text(text)
    subprocess.run(["git", "add", "tests"], cwd=base, check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "--allow-empty",
            "-m",
            "Base",
        ],
        cwd=base,
        check=True,
        capture_output=True,
    )
    record = tmp_path / "record.json"
    subprocess.run(
        [sys.executable, "-m", "tests.count_floor", "--record", str(record), "--head", str(base)],
        cwd=_ROOT,
        check=True,
        capture_output=True,
    )
    if recorded is not None:
        record.write_text(recorded(record.read_text()))
    head = _tree(tmp_path / "head", head_files)
    runs_before = (base / ".collection-runs").read_text()
    result = subprocess.run(
        [sys.executable, "-m", "tests.count_floor", "--base", str(base), "--head", str(head)],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        env={**os.environ, "TEST_COUNT_BASE_RECORD": str(record), **(environ or {})},
    )
    return result, (base / ".collection-runs").read_text() != runs_before


def test_a_matching_dev_record_grades_the_base_without_collecting_it(tmp_path):
    result, collected = _recorded_floor(tmp_path, {}, {"test_a.py": SUITE}, None)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "head collects 3 tests, base floor 3" in result.stdout
    assert "base count read from the dev record" in result.stdout
    assert not collected


def test_a_dev_record_keeps_the_floor_red_on_a_lost_test(tmp_path):
    result, collected = _recorded_floor(tmp_path, {}, {"test_a.py": OTHER}, None)
    assert result.returncode == 1
    assert "tests/test_a.py::test_a is in the base and not in the head" in result.stdout
    assert not collected


@pytest.mark.parametrize(
    "recorded",
    [
        lambda text: json.dumps({**json.loads(text), "identity": "0" * 64}),
        lambda text: "not json",
        lambda text: json.dumps({"identity": json.loads(text)["identity"]}),
        lambda text: json.dumps({**json.loads(text), "nodeids": [3]}),
    ],
    ids=["other-tree-or-environment", "unreadable", "no-tests", "invalid-tests"],
)
def test_a_record_that_does_not_match_the_base_falls_back_to_collecting_it(tmp_path, recorded):
    result, collected = _recorded_floor(tmp_path, {"test_a.py": SUITE + OTHER}, {"test_a.py": SUITE}, recorded)
    assert collected
    assert result.returncode == 1
    assert "no dev record matches the base, so it is collected" in result.stdout
    assert "base floor 4" in result.stdout


def test_a_missing_record_file_collects_the_base(tmp_path):
    result, collected = _recorded_floor(
        tmp_path, {}, {"test_a.py": SUITE}, None, {"TEST_COUNT_BASE_RECORD": str(tmp_path / "absent.json")}
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert collected


def test_a_record_from_an_exported_collection_matches_a_collected_record_without_collecting(tmp_path):
    tree = _collection_tree(tmp_path)
    collected = tmp_path / "collected.json"
    collected_record, exported_record = tmp_path / "collected-record.json", tmp_path / "exported-record.json"
    record = [sys.executable, "-m", "tests.count_floor", "--head", str(tree)]
    subprocess.run([*record, "--record", str(collected_record)], cwd=_ROOT, check=True, capture_output=True)
    collected.write_text(json.dumps(json.loads(collected_record.read_text())["nodeids"]))
    runs_before = (tree / ".collection-runs").read_text()
    result = subprocess.run(
        [*record, "--record", str(exported_record), "--collected", str(collected)],
        cwd=_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert (tree / ".collection-runs").read_text() == runs_before
    assert json.loads(exported_record.read_text()) == json.loads(collected_record.read_text())


@pytest.mark.parametrize("exported", ["[]", "[3]", "{}", "not json", None])
def test_an_unusable_exported_collection_writes_no_record(tmp_path, exported):
    tree = _collection_tree(tmp_path)
    collected, record = tmp_path / "collected.json", tmp_path / "record.json"
    if exported is not None:
        collected.write_text(exported)
    result = subprocess.run(
        [sys.executable, "-m", "tests.count_floor", "--head", str(tree), "--record", str(record)]
        + ["--collected", str(collected)],
        cwd=_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert not record.exists()


@pytest.mark.parametrize(
    "artifact, picked",
    [
        ({}, True),
        ({"head_branch": "ci-plant"}, False),
        ({"head_sha": "f" * 40}, False),
        ({"head_repository_id": 2}, False),
        ({"expired": True}, False),
        ({"head_branch": "gh-readonly-queue/dev/pr-1-" + "b" * 40}, True),
        ({"head_branch": "gh-readonly-queue/main/pr-1-" + "b" * 40}, False),
    ],
    ids=["dev-push", "pull-request-branch", "other-commit", "fork", "expired", "dev-queue-group", "main-queue-group"],
)
def test_only_an_artifact_from_dev_for_the_exact_base_commit_is_trusted(artifact, picked):
    from tests.count_floor import dev_record

    run = {"head_branch": "dev", "head_sha": "a" * 40, "repository_id": 1, "head_repository_id": 1}
    expired = artifact.pop("expired", False)
    listing = {
        "artifacts": [
            {"id": 7, "expired": False, "workflow_run": {**run, "head_branch": "ci-plant"}},
            {"id": 9, "expired": expired, "workflow_run": {**run, **artifact}},
        ]
    }
    assert dev_record(listing, "a" * 40) == (9 if picked else None)


def test_head_matching_the_base_count_passes(tmp_path):
    result = _floor(tmp_path, {"test_a.py": SUITE}, {"test_a.py": SUITE})
    assert result.returncode == 0, result.stdout + result.stderr
    assert "head collects 3 tests, base floor 3" in result.stdout


def test_head_adding_tests_passes(tmp_path):
    result = _floor(tmp_path, {"test_a.py": SUITE}, {"test_a.py": SUITE, "test_c.py": OTHER})
    assert result.returncode == 0, result.stdout + result.stderr
    assert "head collects 4 tests, base floor 3" in result.stdout


def test_a_deleted_test_file_is_red_and_names_every_lost_test(tmp_path):
    result = _floor(tmp_path, {"test_a.py": SUITE, "test_c.py": OTHER}, {"test_a.py": SUITE})
    assert result.returncode == 1
    assert "head collects 3 tests, base floor 4" in result.stdout
    assert "tests/test_c.py::test_c is in the base and not in the head" in result.stdout
    assert "::error::" in result.stdout


def test_a_renamed_test_keeps_the_floor(tmp_path):
    renamed = SUITE.replace("def test_a", "def test_renamed")
    result = _floor(tmp_path, {"test_a.py": SUITE}, {"test_a.py": renamed})
    assert result.returncode == 0, result.stdout + result.stderr
    assert "not in the head" not in result.stdout


@pytest.mark.parametrize("side", ["base", "head"])
def test_a_collection_error_on_either_side_is_red(tmp_path, side):
    broken = {
        "test_a.py": SUITE,
        "test_x.py": "import not_a_module_anywhere\n",
        "test_y.py": "raise RuntimeError('another collection exception')\n",
    }
    sides = {"base": {"test_a.py": SUITE}, "head": {"test_a.py": SUITE}}
    sides[side] = broken
    result = _floor(tmp_path, sides["base"], sides["head"])
    assert result.returncode == 1
    assert f"::error::Collecting the {side} tests failed" in result.stdout
    output = result.stdout + result.stderr
    assert "tests/test_x.py" in output
    assert "ModuleNotFoundError: No module named 'not_a_module_anywhere'" in output
    assert "tests/test_y.py" in output
    assert "RuntimeError: another collection exception" in output


SUBJECT = "def subject():\n    return 1\n"
KEPT = "def other():\n    return 2\n"
SUBJECT_TESTS = """
from pkg import mod

def test_subject():
    assert mod.subject() == 1

def test_other():
    assert mod.other() == 2
"""
OTHER_TEST = """
from pkg import mod

def test_other():
    assert mod.other() == 2
"""


def _package(**modules: str) -> dict[str, str]:
    return {"pkg/__init__.py": "", **{f"pkg/{name}.py": text for name, text in modules.items()}}


def _lose_test_subject(tmp_path, head_sources: dict[str, str], head_tests: dict[str, str] | None = None):
    return _floor(
        tmp_path,
        {"test_mod.py": SUBJECT_TESTS},
        head_tests or {"test_mod.py": OTHER_TEST},
        {"base": _package(mod=SUBJECT + "\n\n" + KEPT), "head": head_sources},
    )


def test_a_lost_test_whose_subject_survives_is_red(tmp_path):
    result = _lose_test_subject(tmp_path, _package(mod=SUBJECT + "\n\n" + KEPT))
    assert result.returncode == 1
    assert "tests/test_mod.py::test_subject is in the base and not in the head" in result.stdout


def test_a_lost_test_whose_subject_moved_to_another_module_is_red(tmp_path):
    result = _lose_test_subject(tmp_path, _package(mod=KEPT, elsewhere=SUBJECT))
    assert result.returncode == 1
    assert "tests/test_mod.py::test_subject is in the base and not in the head" in result.stdout


def test_a_lost_test_whose_subject_was_deleted_is_admitted(tmp_path):
    result = _lose_test_subject(tmp_path, _package(mod=KEPT))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "tests/test_mod.py::test_subject is admitted: the head deletes pkg.mod.subject, which it referenced" in (
        result.stdout
    )
    assert "head collects 1 tests, base floor 1" in result.stdout


@pytest.mark.parametrize("deleted", [True, False], ids=["deleted", "retained"])
@pytest.mark.parametrize(
    "collision",
    [SUBJECT, "class Runner:\n    def subject(self):\n        return 3\n", "subject = 3\n"],
    ids=["function", "method", "constant"],
)
def test_a_preexisting_unrelated_name_does_not_hide_subject_deletion(tmp_path, deleted, collision):
    result = _floor(
        tmp_path,
        {"test_mod.py": SUBJECT_TESTS},
        {"test_mod.py": OTHER_TEST},
        {
            "base": _package(mod=SUBJECT + "\n\n" + KEPT, elsewhere=collision),
            "head": _package(mod=("" if deleted else SUBJECT) + "\n\n" + KEPT, elsewhere=collision),
        },
    )
    assert result.returncode == (0 if deleted else 1), result.stdout + result.stderr
    assert ("test_subject is admitted" in result.stdout) == deleted


@pytest.mark.parametrize("deleted", [True, False], ids=["deleted", "retained"])
@pytest.mark.parametrize("called", [True, False], ids=["called", "uncalled"])
def test_only_a_called_local_helper_can_explain_a_deleted_subject(tmp_path, deleted, called):
    tests = (
        "from pkg.mod import subject, other\n\n"
        "def first():\n    return second()\n\n"
        "def second():\n    return subject()\n\n"
        "def test_subject():\n    assert " + ("first() == 1" if called else "True") + "\n\n"
        "def test_other():\n    assert other() == 2\n"
    )
    result = _floor(
        tmp_path,
        {"test_mod.py": tests},
        {"test_mod.py": OTHER_TEST},
        {
            "base": _package(mod=SUBJECT + "\n\n" + KEPT),
            "head": _package(mod=("" if deleted else SUBJECT) + "\n\n" + KEPT),
        },
    )
    admitted = called and deleted
    assert result.returncode == (0 if admitted else 1), result.stdout + result.stderr
    assert ("test_subject is admitted" in result.stdout) == admitted


@pytest.mark.parametrize("deleted", [True, False], ids=["deleted", "retained"])
@pytest.mark.parametrize("executable", [True, False], ids=["code", "data"])
def test_a_deleted_executable_fixture_explains_only_its_own_lost_test(tmp_path, deleted, executable):
    fixture = json.dumps({"steps": [{"run": "printf retired"}]} if executable else {"label": "retired"})
    tests = (
        "import json\nfrom pathlib import Path\n\n"
        "_ROOT = Path(__file__).resolve().parents[1]\n\n"
        "def fixture():\n    return json.loads((_ROOT / 'tests/fixtures/watch.json').read_text())\n\n"
        "def test_fixture():\n    assert fixture()\n\n"
        "def test_kept():\n    assert True\n"
    )
    result = _floor(
        tmp_path,
        {"test_mod.py": tests},
        {"test_mod.py": "def test_kept():\n    assert True\n"},
        {
            "base": {"tests/fixtures/watch.json": fixture},
            "head": {} if deleted else {"tests/fixtures/watch.json": fixture},
        },
    )
    admitted = deleted and executable
    assert result.returncode == (0 if admitted else 1), result.stdout + result.stderr
    assert ("test_fixture is admitted" in result.stdout) == admitted


@pytest.mark.parametrize("deleted", [True, False], ids=["deleted", "retained"])
@pytest.mark.parametrize("run_name", ["__main__", "other"], ids=["entrypoint", "import"])
def test_a_runpy_entrypoint_explains_only_deleted_executed_code(tmp_path, deleted, run_name):
    entrypoint = SUBJECT.replace("subject", "main")
    tests = (
        "import runpy\nfrom pkg.mod import other\n\n"
        "def test_subject():\n    assert runpy.run_module('pkg.mod', run_name=" + repr(run_name) + ")\n\n"
        "def test_other():\n    assert other() == 2\n"
    )
    result = _floor(
        tmp_path,
        {"test_mod.py": tests},
        {"test_mod.py": OTHER_TEST},
        {
            "base": _package(mod=entrypoint + "\n\n" + KEPT + "\nif __name__ == '__main__':\n    main()\n"),
            "head": _package(mod=("" if deleted else entrypoint) + "\n\n" + KEPT),
        },
    )
    admitted = deleted and run_name == "__main__"
    assert result.returncode == (0 if admitted else 1), result.stdout + result.stderr
    assert ("test_subject is admitted" in result.stdout) == admitted


@pytest.mark.parametrize(
    "survivor",
    [
        "class Runner:\n    def subject(self):\n        return 1\n",
        "from pkg.mod import other as subject\n",
        "subject = 1\n",
    ],
    ids=["method", "import", "assignment"],
)
def test_a_subject_name_still_bound_in_the_head_admits_nothing(tmp_path, survivor):
    result = _lose_test_subject(tmp_path, _package(mod=KEPT, elsewhere=survivor))
    assert result.returncode == 1
    assert "tests/test_mod.py::test_subject is in the base and not in the head" in result.stdout


def test_a_subject_moved_into_the_tests_tree_admits_nothing(tmp_path):
    head_tests = {"test_mod.py": OTHER_TEST, "floor_helpers.py": SUBJECT}
    result = _lose_test_subject(tmp_path, _package(mod=KEPT), head_tests)
    assert result.returncode == 1
    assert "tests/test_mod.py::test_subject is in the base and not in the head" in result.stdout


@pytest.mark.parametrize(
    "sources",
    [
        {**_package(mod=KEPT), "broken.py": "def (:\n"},
        _package(mod=KEPT + "\nfrom pkg.elsewhere import *\n", elsewhere=SUBJECT.replace("subject", "renamed")),
    ],
    ids=["unparseable", "star-import"],
)
def test_a_head_file_that_hides_its_names_admits_nothing(tmp_path, sources):
    result = _lose_test_subject(tmp_path, sources)
    assert result.returncode == 1
    assert "hides its names in the head (unparseable or a star import), so no lost test is admitted" in result.stdout
    assert "tests/test_mod.py::test_subject is in the base and not in the head" in result.stdout


@pytest.mark.parametrize(
    "survivor",
    [
        "try:\n    pass\nexcept ValueError as subject:\n    pass\n",
        "match 1:\n    case subject:\n        pass\n",
    ],
    ids=["except", "match"],
)
def test_a_subject_name_rebound_by_except_or_match_admits_nothing(tmp_path, survivor):
    result = _lose_test_subject(tmp_path, _package(mod=KEPT, elsewhere=survivor))
    assert result.returncode == 1
    assert "tests/test_mod.py::test_subject is in the base and not in the head" in result.stdout


def test_an_attribute_of_a_non_module_never_matches_a_deleted_definition(tmp_path):
    base = "def test_lookup():\n    assert {1: 2}.get(1)\n"
    result = _floor(
        tmp_path,
        {"test_mod.py": base, "test_c.py": OTHER},
        {"test_c.py": OTHER},
        {"base": _package(mod="def get():\n    return 1\n"), "head": _package(mod="")},
    )
    assert result.returncode == 1
    assert "tests/test_mod.py::test_lookup is in the base and not in the head" in result.stdout


def test_a_lost_class_test_whose_imported_subject_was_deleted_is_admitted(tmp_path):
    base = "from pkg.mod import Subject\n\n\nclass TestSubject:\n    def test_it(self):\n        assert Subject\n"
    result = _floor(
        tmp_path,
        {"test_mod.py": base, "test_c.py": OTHER},
        {"test_c.py": OTHER},
        {"base": _package(mod="class Subject:\n    pass\n"), "head": _package(mod="")},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "tests/test_mod.py::TestSubject::test_it is admitted: the head deletes pkg.mod.Subject" in result.stdout


def test_a_subject_deleted_only_from_the_tests_tree_admits_nothing(tmp_path):
    base = "from floor_helpers import subject\n\n\ndef test_subject():\n    assert subject()\n"
    result = _floor(
        tmp_path,
        {"test_mod.py": base, "floor_helpers.py": SUBJECT, "test_c.py": OTHER},
        {"test_c.py": OTHER},
    )
    assert result.returncode == 1
    assert "tests/test_mod.py::test_subject is in the base and not in the head" in result.stdout


def test_a_deleted_subject_admits_only_the_tests_that_referenced_it(tmp_path):
    result = _floor(
        tmp_path,
        {"test_mod.py": SUBJECT_TESTS, "test_c.py": OTHER},
        {"test_c.py": OTHER},
        {"base": _package(mod=SUBJECT + "\n\n" + KEPT), "head": _package(mod=KEPT)},
    )
    assert result.returncode == 1
    assert "tests/test_mod.py::test_subject is admitted" in result.stdout
    assert "head collects 1 tests, base floor 2" in result.stdout
    assert "tests/test_mod.py::test_other is in the base and not in the head" in result.stdout


@pytest.mark.parametrize("constant", ['FORM = "queue"\n', 'FORM: str = "queue"\n'], ids=["assign", "annotated"])
@pytest.mark.parametrize(
    "test",
    [
        "from pkg.mod import FORM\n\n\ndef test_form():\n    assert FORM\n",
        "from pkg import mod\n\n\ndef test_form():\n    assert mod.FORM\n",
    ],
    ids=["imported", "attribute"],
)
def test_a_lost_test_whose_constant_the_head_deletes_is_admitted(tmp_path, constant, test):
    result = _floor(
        tmp_path,
        {"test_mod.py": test, "test_c.py": OTHER},
        {"test_c.py": OTHER},
        {"base": _package(mod=constant + KEPT), "head": _package(mod=KEPT)},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert (
        "tests/test_mod.py::test_form is admitted: the head deletes pkg.mod.FORM, which it referenced" in result.stdout
    )


def test_a_lost_test_whose_constant_the_head_keeps_is_red(tmp_path):
    test = "from pkg import mod\n\n\ndef test_form():\n    assert mod.FORM\n"
    source = _package(mod='FORM = "queue"\n' + KEPT)
    result = _floor(
        tmp_path, {"test_mod.py": test, "test_c.py": OTHER}, {"test_c.py": OTHER}, {"base": source, "head": source}
    )
    assert result.returncode == 1
    assert "tests/test_mod.py::test_form is in the base and not in the head" in result.stdout


BRANCHED = """
def steps(queue=False, selected=False):
    run = "plain"
    if queue:
        run += " queue"
    return run + " selected" if selected else run
"""
RETAINED = 'def steps(selected=False):\n    return "plain selected" if selected else "plain"\n'
BRANCH_TESTS = """
from pkg import mod

def test_queue():
    assert mod.steps(queue=True) == "plain queue"

def test_default():
    assert mod.steps() == "plain"

def test_selected():
    assert mod.steps(selected=True) == "plain selected"
"""
SELECTED_TEST = """
from pkg import mod

def test_selected():
    assert mod.steps(selected=True) == "plain selected"
"""


def _lose_branch_tests(tmp_path, base_source: str, head_sources: dict[str, str]):
    return _floor(
        tmp_path,
        {"test_mod.py": BRANCH_TESTS},
        {"test_mod.py": SELECTED_TEST},
        {"base": _package(mod=base_source), "head": head_sources},
    )


@pytest.mark.parametrize(
    "base",
    [
        BRANCHED,
        'def steps(queue=False, selected=False):\n    return "plain queue" if queue else "plain"\n',
        'def steps(queue=False, selected=False):\n    while queue:\n        return "plain queue"\n    return "plain"\n',
    ],
    ids=["if", "conditional", "while"],
)
def test_a_lost_test_whose_keyword_branch_the_head_deletes_is_admitted(tmp_path, base):
    result = _lose_branch_tests(tmp_path, base, _package(mod=RETAINED))
    assert result.returncode == 1
    assert "tests/test_mod.py::test_queue is admitted: the head deletes pkg.mod.steps(queue), which it referenced" in (
        result.stdout
    )
    assert "head collects 1 tests, base floor 2" in result.stdout
    assert "tests/test_mod.py::test_default is in the base and not in the head" in result.stdout


@pytest.mark.parametrize(
    "head",
    [
        _package(mod=BRANCHED),
        _package(mod='def steps(selected=False, **options):\n    return "plain"\n'),
        _package(mod=KEPT, elsewhere=RETAINED),
        _package(mod=BRANCHED.replace("queue=False, ", "queue=False, /, ")),
        _package(mod=BRANCHED.replace("queue", "lane")),
        _package(mod=BRANCHED.replace("queue=False, selected=False", "*queue, selected=False")),
        _package(mod="if True:\n" + RETAINED.replace("\n    ", "\n        ").replace("def", "    def")),
    ],
    ids=["kept", "kwargs", "moved", "positional", "renamed", "variadic", "nested"],
)
def test_a_keyword_the_head_still_accepts_moved_or_renamed_admits_nothing(tmp_path, head):
    result = _lose_branch_tests(tmp_path, BRANCHED, head)
    assert result.returncode == 1
    assert "tests/test_mod.py::test_queue is in the base and not in the head" in result.stdout


@pytest.mark.parametrize(
    "base",
    [
        'def steps(queue=False, selected=False):\n    return f"plain {queue} {selected}"\n',
        BRANCHED.replace('run = "plain"', 'run = "plain"\n    queue = queue or selected'),
    ],
    ids=["unbranched", "rebound"],
)
def test_a_removed_parameter_that_selected_no_branch_admits_nothing(tmp_path, base):
    result = _lose_branch_tests(tmp_path, base, _package(mod=RETAINED))
    assert result.returncode == 1
    assert "tests/test_mod.py::test_queue is in the base and not in the head" in result.stdout


def test_a_head_file_that_hides_its_names_admits_no_deleted_branch(tmp_path):
    result = _lose_branch_tests(tmp_path, BRANCHED, {**_package(mod=RETAINED), "broken.py": "def (:\n"})
    assert result.returncode == 1
    assert "hides its names in the head (unparseable or a star import), so no lost test is admitted" in result.stdout
    assert "tests/test_mod.py::test_queue is in the base and not in the head" in result.stdout
