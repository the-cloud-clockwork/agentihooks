"""A checkout holding only the project local enforcements store reads clean to the stop gate and the dev sync."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from hooks.context import enforcement
from scripts.gates import push_stop

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[2]
BRANCH = "engineer-100001-0001"
IDENTITY = ("-c", "user.name=t", "-c", "user.email=t@example.com")


def git(path: Path, *args: str) -> str:
    """Run git in path and return its stripped output."""
    done = subprocess.run(["git", "-C", str(path), *IDENTITY, *args], capture_output=True, text=True, check=True)
    return done.stdout.strip()


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    """A committed checkout carrying the repository's own ignore rules and a local enforcement written by agentihooks."""
    tree = tmp_path / BRANCH
    tree.mkdir()
    git(tree, "init", "-q", "-b", BRANCH)
    shutil.copy(REPO / ".gitignore", tree / ".gitignore")
    git(tree, "add", ".gitignore")
    git(tree, "commit", "-q", "-m", "seed")
    assert enforcement.add_enforcement("stay on task", 5, local=True, cwd=tree)
    return tree


@pytest.mark.feature("local-enforcements-store-ignored")
def test_the_repo_ignore_rules_cover_the_local_enforcements_store(checkout: Path) -> None:
    store = enforcement._local_store_path(checkout)
    assert store.is_file()
    assert git(checkout, "status", "--porcelain", "--untracked-files=all") == ""
    names = (".agentihooks/enforcements.json", ".agentihooks/enforcements.json.tmp")
    assert git(checkout, "check-ignore", *names).splitlines() == list(names)


@pytest.mark.feature("local-enforcements-store-ignored")
def test_the_stop_gate_reads_a_checkout_holding_only_the_store_as_clean(checkout: Path) -> None:
    tree = push_stop.inspect(checkout, re.compile(re.escape(BRANCH)))
    assert tree is not None
    assert tree.dirty is False
    assert enforcement._local_store_path(checkout).is_file()


@pytest.mark.feature("local-enforcements-store-ignored")
def test_the_dev_sync_after_wt_done_reads_a_checkout_holding_only_the_store_as_clean(checkout: Path) -> None:
    assert git(checkout, "status", "--porcelain", "--untracked-files=no") == ""
    assert git(checkout, "status", "--porcelain") == ""
    assert enforcement._local_store_path(checkout).is_file()
