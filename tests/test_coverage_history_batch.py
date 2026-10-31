import subprocess
from pathlib import Path
from typing import Any

import pytest

from tests import coverage_history

pytestmark = pytest.mark.unit

FILES = {
    "hooks/a.py": "a = 1\n",
    "hooks/crlf.py": "a = 1\r\nb = 2\r\n",
    "hooks/bare.py": "a = 1\rb = 2",
    "scripts/name with space.py": "value = 'é'\n",
    "tests/test_x.py": "",
}
PATHS = [*FILES, "hooks/absent.py", "hooks", "absent dir/x 12"]


def _commit(repo: Path, files: dict[str, str]) -> str:
    for name, text in files.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_bytes(text.encode())
    for args in (["add", "-A"], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "c"]):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    return coverage_history.git("rev-parse", "HEAD", cwd=repo).strip()


def _git_show(repo: Path, commit: str, path: str) -> str | None:
    shown = subprocess.run(["git", "show", f"{commit}:{path}"], cwd=repo, capture_output=True, text=True)
    return shown.stdout if shown.returncode == 0 else None


@pytest.fixture
def repo(tmp_path: Path) -> tuple[Path, str]:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    return tmp_path, _commit(tmp_path, FILES)


@pytest.fixture
def reads(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    started: list[list[str]] = []
    original = subprocess.Popen

    def counted(*args: Any, **kwargs: Any) -> object:
        command = [str(part) for part in args[0]]
        if command[1] in ("show", "cat-file"):
            started.append(command)
        return original(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", counted)
    return started


def test_every_source_read_of_a_revision_shares_one_git_process(repo: tuple[Path, str], reads: list[list[str]]) -> None:
    root, commit = repo
    source = coverage_history._show(root, commit)
    for path in PATHS:
        source(path)
    assert len(reads) == 1


def test_renamed_reads_each_revision_through_one_git_process(repo: tuple[Path, str], reads: list[list[str]]) -> None:
    root, base = repo
    for name in ("hooks/a.py", "hooks/crlf.py", "hooks/bare.py"):
        (root / name).unlink()
    _commit(root, {"scripts/a.py": FILES["hooks/a.py"], "scripts/new.py": "n = 1\n", "scripts/other.py": "o = 1\n"})
    reads.clear()
    moved = coverage_history.renamed(root, base)
    assert moved["hooks/a.py"] == "scripts/a.py"
    assert len(reads) == 2


def test_batched_reads_return_what_git_show_returned(repo: tuple[Path, str]) -> None:
    root, commit = repo
    files = [path for path in PATHS if path != "hooks"]
    expected = {path: _git_show(root, commit, path) for path in files}
    source = coverage_history._show(root, commit)
    assert {path: source(path) for path in files} == expected
    assert expected["hooks/crlf.py"] == "a = 1\nb = 2\n"
    assert expected["hooks/absent.py"] is None


def test_a_symbolic_revision_reads_its_own_tree(repo: tuple[Path, str]) -> None:
    root, first = repo
    _commit(root, {"hooks/a.py": "a = 2\n"})
    assert coverage_history._show(root, first)("hooks/a.py") == "a = 1\n"
    assert coverage_history._show(root, "HEAD")("hooks/a.py") == "a = 2\n"
    assert coverage_history._show(root, "HEAD~1")("hooks/a.py") == "a = 1\n"
