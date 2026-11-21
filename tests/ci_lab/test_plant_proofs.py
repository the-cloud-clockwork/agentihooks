import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from scripts import no_comments
from scripts.ci_lab import plant_proofs

pytestmark = [pytest.mark.unit, pytest.mark.feature("lab-static-plant-proofs")]
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
            content = subprocess.run(
                ["git", "-C", str(SOURCE), "show", f"HEAD:{edit['path']}"], check=True, capture_output=True
            ).stdout
            target.write_bytes(content)
    git(root, "add", ".")
    git(root, "commit", "-qm", "Seed plant inputs")
    return root


@pytest.mark.parametrize(
    "name,path", [("lint", "scripts/ci_lab/head_notice.py"), ("gate-script", "scripts/no_comments.py")]
)
@pytest.mark.feature("lab-static-plant-proofs")
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


@pytest.mark.parametrize("name", ["lint", "gate-script"])
def test_a_pair_names_its_gate_reason_and_fixed_commit_metadata(repository: Path, name: str) -> None:
    pair = plant_proofs.prepare(repository, name, "HEAD")
    assert pair.name == name
    assert pair.gate == "lint"
    assert (
        pair.reason
        == {
            "lint": "I001: import block is unsorted",
            "gate-script": "forbidden comment: the base grader refuses the head's invented directive",
        }[name]
    )
    for revision, subject in [(pair.green, f"CI lab {name} twin"), (pair.red, f"CI lab {name} plant")]:
        commit = git(repository, "cat-file", "-p", revision)
        assert "author CI lab plant <ci-lab@example.invalid> 946684800 +0000\n" in commit
        assert "committer CI lab plant <ci-lab@example.invalid> 946684800 +0000\n" in commit
        assert commit.split("\n\n", 1)[1] == subject


@pytest.mark.parametrize("name", ["", "Lint", "../lint", "lint/other", "lint\n"])
def test_a_plant_name_cannot_escape_its_catalog(repository: Path, name: str) -> None:
    with pytest.raises(ValueError, match="^a plant name contains lowercase words and hyphens$"):
        plant_proofs.prepare(repository, name, "HEAD")


@pytest.mark.parametrize("source", ["missing\n", "before\nbefore\n"])
def test_an_edit_with_zero_or_two_matches_is_refused(repository: Path, source: str) -> None:
    target = repository / "sample.py"
    target.write_text(source)
    git(repository, "add", "sample.py")
    git(repository, "commit", "-qm", "Seed exact edit")
    fixture = repository / "tests/ci_lab/plants/lint.json"
    fixture.write_text(
        json.dumps(
            {
                "gate": "lint",
                "reason": "exact edit",
                "edits": [
                    {"path": "sample.py", "before": "before", "after": "after"},
                ],
            }
        )
    )
    with pytest.raises(ValueError, match="^lint: sample.py must match its edit exactly once$"):
        plant_proofs.prepare(repository, "lint", "HEAD")


@pytest.mark.parametrize("path", ["sample.py", "-sample.py"])
def test_ordered_edits_preserve_all_other_bytes_and_the_executable_mode(repository: Path, path: str) -> None:
    target = repository / path
    target.write_text("before\nuntouched\n")
    target.chmod(0o755)
    git(repository, "add", "--", path)
    git(repository, "commit", "-qm", "Seed executable")
    fixture = repository / "tests/ci_lab/plants/lint.json"
    fixture.write_text(
        json.dumps(
            {
                "gate": "lint",
                "reason": "exact edit",
                "edits": [
                    {"path": path, "before": "before", "after": "middle"},
                    {"path": path, "before": "middle", "after": "after"},
                ],
            }
        )
    )
    pair = plant_proofs.prepare(repository, "lint", "HEAD")
    assert git(repository, "show", f"{pair.red}:{path}") == "after\nuntouched"
    assert git(repository, "ls-tree", pair.red, "--", path).startswith("100755 blob ")
    assert target.read_text() == "before\nuntouched\n"
    assert not (repository / "None").exists()


def test_git_failures_are_raised_and_every_git_process_has_a_deadline(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_run = subprocess.run
    directory = repository.parent / "alternate-index"
    directory.mkdir()
    monkeypatch.setenv("GIT_INDEX_FILE", str(directory / "index"))

    def bounded(arguments: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert kwargs["timeout"] == 60
        assert kwargs["check"] is True
        if arguments[3] == "rev-parse" and arguments[-1].endswith("^{commit}"):
            assert arguments[4] == "--verify"
        if index := kwargs.get("env", {}).get("GIT_INDEX_FILE"):
            if Path(index) != directory / "index":
                assert Path(index).parent.parent == directory
        return real_run(arguments, **kwargs)

    with patch.object(plant_proofs.subprocess, "run", side_effect=bounded):
        plant_proofs.prepare(repository, "lint", "HEAD")
        with pytest.raises(subprocess.CalledProcessError):
            plant_proofs.prepare(repository, "lint", "missing-revision")
    assert not (directory / "index").exists()


@pytest.mark.feature("lab-static-plant-proofs")
def test_the_lint_fixture_turns_only_import_sorting_red(repository: Path) -> None:
    pair = plant_proofs.prepare(repository, "lint", "HEAD")
    target = repository / "scripts/ci_lab/head_notice.py"
    command = [sys.executable, "-m", "ruff", "check", "--select", "I001", str(target)]
    green = subprocess.run(command, capture_output=True, text=True, check=False)
    assert green.returncode == 0, green.stdout + green.stderr
    target.write_text(git(repository, "show", f"{pair.red}:scripts/ci_lab/head_notice.py") + "\n")
    red = subprocess.run(command, capture_output=True, text=True, check=False)
    assert red.returncode == 1, red.stdout + red.stderr
    assert "I001" in red.stdout


@pytest.mark.feature("lab-static-plant-proofs")
def test_the_base_comment_grader_refuses_the_heads_invented_directive(repository: Path, tmp_path: Path) -> None:
    pair = plant_proofs.prepare(repository, "gate-script", "HEAD")
    assert no_comments.grade(repository, repository, False) == []
    head = tmp_path / "planted"
    shutil.copytree(repository, head)
    path = head / "scripts/no_comments.py"
    path.write_text(git(repository, "show", f"{pair.red}:scripts/no_comments.py") + "\n")
    errors = no_comments.grade(repository, head, False)
    assert len(errors) == 1
    assert errors[0].startswith("scripts/no_comments.py:")
    assert errors[0].endswith(": forbidden comment")
    answer = subprocess.run(
        [sys.executable, str(path), "--base", str(repository), "--head", str(head)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert answer.returncode == 0, answer.stdout + answer.stderr
