import json
import shutil
import subprocess
from pathlib import Path

import pytest

from scripts.ci_lab import plant_proofs

pytestmark = pytest.mark.unit
SOURCE = Path(__file__).resolve().parents[2]


def git(root: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *arguments], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.name", "Plant proof")
    git(root, "config", "user.email", "plant@example.invalid")
    catalog = Path("tests/ci_lab/plants")
    (root / catalog.parent).mkdir(parents=True)
    shutil.copytree(SOURCE / catalog, root / catalog)
    for fixture in (root / catalog).glob("*.json"):
        for edit in json.loads(fixture.read_text())["edits"]:
            target = root / edit["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(SOURCE / edit["path"], target)
    git(root, "add", ".")
    git(root, "commit", "-qm", "Seed plant inputs")
    return root


@pytest.mark.parametrize(
    "name,path", [("lint", "scripts/ci_lab/head_notice.py"), ("gate-script", "scripts/no_comments.py")]
)
def test_a_plant_has_reproducible_commits_and_preserves_the_checkout(repository: Path, name: str, path: str) -> None:
    base = git(repository, "rev-parse", "HEAD")
    original_index = git(repository, "ls-files", "--stage")
    original_source = (repository / path).read_text()
    pair = plant_proofs.prepare(repository, name, base)
    assert pair == plant_proofs.prepare(repository, name, base)
    assert pair.base == base
    assert git(repository, "rev-parse", f"{pair.green}^{{tree}}") == git(repository, "rev-parse", f"{base}^{{tree}}")
    assert git(repository, "rev-parse", f"{pair.green}^") == base
    assert git(repository, "rev-parse", f"{pair.red}^") == pair.green
    assert git(repository, "diff", "--name-only", pair.green, pair.red) == path
    assert git(repository, "show", f"{pair.red}:{path}") != original_source.strip()
    assert git(repository, "rev-parse", "HEAD") == base
    assert git(repository, "ls-files", "--stage") == original_index
    assert (repository / path).read_text() == original_source
