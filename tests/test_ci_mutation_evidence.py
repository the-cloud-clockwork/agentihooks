import shutil
from pathlib import Path

import pytest
import yaml

from scripts.ci_mutation.evidence import main, stage

_ROOT = Path(__file__).resolve().parents[1]
_INPUTS = (
    "hooks/subject.py",
    "scripts/tool.py",
    "tests/test_subject.py",
    "docs/guide.md",
    "fixtures/payload.txt",
    ".github/workflows/other.yml",
    "README.md",
    ".test_durations",
)
_DIAGNOSTICS = (
    "changed-lines.json",
    "pyproject.toml",
    "run.log",
    "report.json",
    ".coverage",
    ".stats/records.json",
    "unknown-trace/result.txt",
    "hooks/generated.log",
    "mutants/hooks/generated.log",
    "mutants/pyproject.toml",
    "mutants/mutmut-stats.json",
    "mutants/.coverage.stats",
    "mutants/unknown-trace/result.txt",
)
_CACHES = (".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache")


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def _files(path: Path) -> dict[str, bytes]:
    return {str(item.relative_to(path)): item.read_bytes() for item in path.rglob("*") if item.is_file()}


def _inputs(tmp_path: Path, nested: bool = True) -> tuple[Path, Path, Path, list[Path]]:
    root = tmp_path / "checkout"
    for name in (*_INPUTS, "pyproject.toml"):
        _write(root / name, "copied source\n" * 1024)
    source = tmp_path / "stats"
    workspaces = [Path("1/b-group"), Path("0/a-group")] if nested else [Path("b-group"), Path("a-group")]
    for workspace in workspaces:
        work = source / workspace
        shutil.copytree(root, work)
        shutil.copytree(root, work / "mutants")
        for name in _DIAGNOSTICS:
            _write(work / name, f"{workspace}: {name}\n")
        for name in _CACHES:
            _write(work / name / "discard", "disposable\n")
            _write(work / "mutants" / name / "discard", "disposable\n")
        _write(work / "compiled.pyc", "disposable\n")
    _write(source / "report.json", "aggregate diagnostics\n")
    return root, source, tmp_path / "evidence", sorted(workspaces)


@pytest.mark.parametrize("nested", [True, False], ids=["plan-parts", "matrix-part"])
@pytest.mark.feature("mutation-stats-failure-upload-set")
def test_failed_statistics_upload_contains_one_trimmed_workspace(tmp_path: Path, nested: bool) -> None:
    root, source, destination, workspaces = _inputs(tmp_path, nested)

    stage(root, source, destination)

    files = _files(destination)
    for name in _INPUTS:
        assert sorted(path for path in files if path.endswith(f"/{name}")) == [str(workspaces[0] / name)]
    assert not any(set(Path(path).parts) & set(_CACHES) or path.endswith(".pyc") for path in files)
    assert sum(map(len, files.values())) < sum(map(len, _files(source).values())) / 2


@pytest.mark.feature("mutation-stats-failure-upload-set")
def test_staging_preserves_every_parts_diagnostics_and_the_producer_workspace(tmp_path: Path) -> None:
    root, source, destination, workspaces = _inputs(tmp_path)
    before = _files(source)

    stage(root, source, destination)

    files = _files(destination)
    assert files["report.json"] == before["report.json"]
    for workspace in workspaces:
        for name in _DIAGNOSTICS:
            relative = str(workspace / name)
            assert files[relative] == before[relative]
    assert _files(source) == before


@pytest.mark.feature("mutation-stats-failure-upload-set")
def test_failure_before_workspace_setup_keeps_available_diagnostics(tmp_path: Path) -> None:
    source = tmp_path / "stats"
    _write(source / "run.log", "setup failed\n")
    _write(source / ".state/failed", "failure\n")
    _write(source / "partial/changed-lines.json", "{}\n")
    destination = tmp_path / "evidence"

    stage(tmp_path / "checkout", source, destination)

    assert _files(destination) == _files(source)


@pytest.mark.feature("mutation-stats-failure-upload-set")
def test_failure_before_output_exists_does_not_create_evidence(tmp_path: Path) -> None:
    destination = tmp_path / "evidence"

    stage(tmp_path / "checkout", tmp_path / "absent", destination)

    assert not destination.exists()


@pytest.mark.feature("mutation-stats-failure-upload-set")
def test_command_line_uses_the_statistics_failure_upload_selection(tmp_path: Path) -> None:
    root, source, destination, workspaces = _inputs(tmp_path)

    main([str(root), str(source), str(destination)])

    assert sorted(str(path.relative_to(destination)) for path in destination.rglob("subject.py")) == [
        str(workspaces[0] / "hooks/subject.py")
    ]


@pytest.mark.parametrize("job", ["mutation-plan", "mutation-stats"])
@pytest.mark.feature("mutation-stats-failure-upload-set")
def test_both_statistics_failure_uploads_use_the_trimmed_evidence_set(job: str) -> None:
    workflow = yaml.safe_load((_ROOT / ".github/workflows/test.yml").read_text())
    steps = workflow["jobs"][job]["steps"]
    upload_index = next(index for index, step in enumerate(steps) if step.get("name") == "Upload stats evidence")
    prepare = steps[upload_index - 1]
    upload = steps[upload_index]
    source = ".mutation-gate-stats" if job == "mutation-plan" else ".mutation-gate"

    assert prepare["name"] == "Prepare stats evidence"
    assert prepare["if"] == upload["if"]
    assert prepare["run"] == f"python -m scripts.ci_mutation.evidence . {source} .mutation-stats-evidence"
    assert upload["with"]["path"] == ".mutation-stats-evidence/"
    assert upload["with"]["include-hidden-files"] is True
    assert upload["with"]["retention-days"] == 7


@pytest.mark.feature("mutation-stats-failure-upload-set")
def test_successful_statistics_and_shard_evidence_keep_their_existing_upload_sets() -> None:
    workflow = yaml.safe_load((_ROOT / ".github/workflows/test.yml").read_text())
    for job, name in (
        ("mutation-plan", "Upload the mutation stats parts"),
        ("mutation-stats", "Upload mutation stats part"),
    ):
        steps = workflow["jobs"][job]["steps"]
        upload = next(step for step in steps if step.get("name") == name)
        assert upload["with"]["path"] == ".mutation-stats/"
        assert upload["with"]["retention-days"] == 1
        cache = next(step for step in steps if step.get("name") == "Save the merged test stats")
        assert cache["with"]["path"] == ".mutation-stats-cache"
    shard = next(
        step
        for step in workflow["jobs"]["mutation-shard"]["steps"]
        if step.get("name") == "Upload complete mutation evidence"
    )
    assert (
        shard["with"]["path"] == "${{ job.status == 'success' && '.mutation-gate/report.json' || '.mutation-gate/' }}"
    )
