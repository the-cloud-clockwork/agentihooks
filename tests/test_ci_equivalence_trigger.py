import subprocess
from pathlib import Path

import pytest

from scripts import ci_equivalence_trigger

pytestmark = pytest.mark.unit


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def _merge_commit(root: Path, base: dict[str, str], head: dict[str, str], removed: tuple[str, ...] = ()) -> None:
    _git(root, "init", "-q", "-b", "dev")
    _git(root, "config", "user.email", "ci@example.com")
    _git(root, "config", "user.name", "ci")
    for name, text in base.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    _git(root, "checkout", "-q", "-b", "head")
    for name, text in head.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)
    for name in removed:
        _git(root, "rm", "-q", name)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "head")
    _git(root, "checkout", "-q", "dev")
    (root / "dev-only.txt").write_text("dev moved on\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "dev")
    _git(root, "merge", "-q", "--no-ff", "-m", "merge", "head")


@pytest.mark.parametrize(
    ("paths", "expected"),
    [
        (["scripts/swarm_ledger/server.py"], True),
        (["tests/swarm_ledger/page_replay.py"], True),
        ([".github/workflows/equivalence.yml"], True),
        (["scripts/ci_equivalence_trigger.py"], True),
        (["README.md", "scripts/swarm/cli.py"], False),
        (["scripts/swarm_ledgerx.py"], False),
        ([], False),
    ],
)
def test_triggered_matches_only_ledger_paths(paths, expected):
    assert ci_equivalence_trigger.triggered(paths) is expected


def test_changed_lists_only_the_pull_request_side_of_the_merge(tmp_path):
    _merge_commit(tmp_path, {"README.md": "a\n"}, {"scripts/swarm_ledger/server.py": "x\n"})
    assert ci_equivalence_trigger.changed(tmp_path) == ["scripts/swarm_ledger/server.py"]


def test_changed_names_both_sides_of_a_rename_out_of_the_ledger(tmp_path):
    _merge_commit(
        tmp_path,
        {"scripts/swarm_ledger/server.py": "same text\n" * 20},
        {"scripts/other.py": "same text\n" * 20},
        removed=("scripts/swarm_ledger/server.py",),
    )
    assert sorted(ci_equivalence_trigger.changed(tmp_path)) == ["scripts/other.py", "scripts/swarm_ledger/server.py"]


def test_main_prints_the_run_output_for_a_ledger_change(tmp_path, capsys):
    _merge_commit(tmp_path, {"README.md": "a\n"}, {"tests/swarm_ledger/case.py": "x\n"})
    assert ci_equivalence_trigger.main(["--root", str(tmp_path)]) == 0
    assert capsys.readouterr().out == "run=true\n"


def test_main_skips_a_change_that_misses_the_ledger(tmp_path, capsys):
    _merge_commit(tmp_path, {"README.md": "a\n"}, {"docs/note.md": "x\n"})
    assert ci_equivalence_trigger.main(["--root", str(tmp_path)]) == 0
    assert capsys.readouterr().out == "run=false\n"
