import json
import subprocess
from pathlib import Path

import pytest

from scripts.ci_mutation.clearances import clearance_path, load_clearances, migrate, write_clearance

KEY = "hooks/example.py:hooks.example.x_f__mutmut_1:fingerprint"
RULING = {"reader": "Independent Standards", "reason": "Same output for every input"}


def test_written_ruling_keeps_the_readable_json_format(tmp_path):
    write_clearance(tmp_path, "identity", {"reader": "Standards", "reason": "Equivalent"})
    assert clearance_path(tmp_path, "identity").read_text() == (
        '{\n  "identity": {\n    "reader": "Standards",\n    "reason": "Equivalent"\n  }\n}\n'
    )


def test_folder_and_legacy_clearances_are_combined(tmp_path):
    (tmp_path / "mutation-cleared.txt").write_text(json.dumps({"legacy": RULING}))
    write_clearance(tmp_path, KEY, RULING)
    assert load_clearances(tmp_path) == {"legacy": RULING, KEY: RULING}
    assert load_clearances(tmp_path / "missing") == {}


def test_each_identity_has_its_own_portable_filename(tmp_path):
    path = clearance_path(tmp_path, KEY)
    assert path.parent == tmp_path / "mutation-clearances"
    assert path.name == "f19cdd4b58f31402296643b974803a6f8b8bb3c462cb7b2846adbf45bc79aa94.json"
    assert path != clearance_path(tmp_path, KEY + "other")


def test_migration_preserves_every_reader_and_reason_and_is_repeatable(tmp_path):
    legacy = {KEY: RULING, "second": {"reader": "Another reader", "reason": "Another reason"}}
    target = tmp_path / "mutation-cleared.txt"
    target.write_text(json.dumps(legacy))
    assert migrate(tmp_path) == 2
    assert json.loads(target.read_text()) == {}
    assert load_clearances(tmp_path) == legacy
    assert migrate(tmp_path) == 0
    for key, ruling in legacy.items():
        assert json.loads(clearance_path(tmp_path, key).read_text()) == {key: ruling}


def test_migration_keeps_new_folder_rulings(tmp_path):
    write_clearance(tmp_path, KEY, RULING)
    (tmp_path / "mutation-cleared.txt").write_text(json.dumps({"second": RULING}))
    assert migrate(tmp_path) == 1
    assert load_clearances(tmp_path) == {KEY: RULING, "second": RULING}


@pytest.mark.parametrize("data", [{}, {KEY: RULING, "second": RULING}])
def test_folder_file_requires_one_identity(tmp_path, data):
    target = clearance_path(tmp_path, KEY)
    target.parent.mkdir()
    target.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="^Mutation clearance file requires one mutant identity$"):
        load_clearances(tmp_path)


def test_folder_filename_is_bound_to_identity(tmp_path):
    target = clearance_path(tmp_path, KEY)
    target.parent.mkdir()
    target.write_text(json.dumps({"wrong identity": RULING}))
    with pytest.raises(ValueError, match="^Mutation clearance filename does not match its identity$"):
        load_clearances(tmp_path)


def test_conflicting_duplicate_rulings_are_refused_without_losing_legacy(tmp_path):
    write_clearance(tmp_path, KEY, RULING)
    legacy = {KEY: {**RULING, "reason": "Conflicting reason"}}
    target = tmp_path / "mutation-cleared.txt"
    target.write_text(json.dumps(legacy))
    with pytest.raises(ValueError, match="Conflicting mutation clearance"):
        migrate(tmp_path)
    assert json.loads(target.read_text()) == legacy


def test_identical_legacy_duplicate_is_allowed(tmp_path):
    write_clearance(tmp_path, KEY, RULING)
    (tmp_path / "mutation-cleared.txt").write_text(json.dumps({KEY: RULING}))
    assert load_clearances(tmp_path) == {KEY: RULING}


def test_separate_branch_additions_merge_without_a_shared_json_conflict(tmp_path):
    repo = tmp_path / "objects.git"
    subprocess.run(["git", "init", "--bare", str(repo)], check=True, capture_output=True)

    def git(*args, data=None):
        result = subprocess.run(
            ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "--git-dir", str(repo), *args],
            input=data,
            text=True,
            capture_output=True,
        )
        assert result.returncode == 0, result.stderr
        return result.stdout.strip()

    def tree(files):
        entries = []
        for name, contents in sorted(files.items()):
            blob = git("hash-object", "-w", "--stdin", data=contents)
            entries.append(f"100644 blob {blob}\t{name}\n")
        return git("mktree", data="".join(entries))

    def commit(files, parent=None):
        root_tree = tree({"mutation-cleared.txt": json.dumps(files)})
        return git("commit-tree", root_tree, *(["-p", parent] if parent else []), data="ruling\n")

    base = commit({})
    left = commit({KEY: RULING}, base)
    right = commit({"other": RULING}, base)
    conflict = git("merge-tree", base, left, right)
    assert "<<<<<<<" in conflict

    def folder_commit(key):
        filename = clearance_path(tmp_path, key).name
        folder_tree = tree({filename: json.dumps({key: RULING})})
        root_tree = git("mktree", data=f"040000 tree {folder_tree}\tmutation-clearances\n")
        return git("commit-tree", root_tree, "-p", base, data="ruling\n")

    left, right = folder_commit(KEY), folder_commit("other")
    assert "<<<<<<<" not in git("merge-tree", base, left, right)
    git("read-tree", "-i", "-m", "--aggressive", base, left, right)
    merged = git("write-tree")
    assert len(git("ls-tree", "-r", "--name-only", merged).splitlines()) == 2
    for key in (KEY, "other"):
        name = clearance_path(tmp_path, key).name
        assert json.loads(git("show", f"{merged}:mutation-clearances/{name}")) == {key: RULING}


def test_repository_folder_records_match_the_combined_loader():
    root = Path(__file__).resolve().parents[1]
    cleared = load_clearances(root)
    for path in (root / "mutation-clearances").glob("*.json"):
        key, ruling = next(iter(json.loads(path.read_text()).items()))
        assert cleared[key] == ruling
