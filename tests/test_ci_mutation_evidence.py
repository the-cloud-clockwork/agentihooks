import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from scripts.ci_mutation import merge

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / ".github/scripts/mutation_evidence.py"
WORKFLOW = ROOT / ".github/workflows/test.yml"
KEEP = "Keep only this shard's own mutation evidence"
OWN = (
    "run.log",
    "report.log",
    "results.json",
    "killers/7a1f",
    "killers/0c2d",
    "mutants/mutmut-stats.json",
    "mutants/hooks/sample.py.meta",
    "mutants/scripts/deep/other.py.meta",
)
WORKSPACE = (
    "changed-lines.json",
    "shared-stats.json",
    "shard-plan.json",
    "pyproject.toml",
    ".test_durations",
    ".git/index",
    "hooks/sample.py",
    "hooks/sample.py.meta",
    "tests/run.log",
    "tests/test_sample.py",
    "docs/results.json",
    "mutants/run.log",
    "mutants/killers/7a1f",
    "mutants/hooks/sample.py",
    "mutants/.git/index",
    "mutants/.pytest_cache/v/cache/lastfailed",
)


def _run(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-I", str(HELPER), *arguments], capture_output=True, text=True, check=False)


def _group(folder: Path, name: str) -> None:
    for relative in (*OWN, *WORKSPACE):
        path = folder / name / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{relative}\n")


def _files(folder: Path) -> list[str]:
    found: list[str] = []
    for here, folders, files in os.walk(folder):
        entries = [*files, *(name for name in folders if (Path(here) / name).is_symlink())]
        found += [(Path(here) / name).relative_to(folder).as_posix() for name in entries]
    return sorted(found)


def _size(names: tuple[str, ...]) -> int:
    return sum(len(name) + 1 for name in names)


def test_each_group_folder_keeps_only_the_files_its_own_run_wrote(tmp_path: Path) -> None:
    gate = tmp_path / ".mutation-gate"
    for name in ("0-l6ouk117", "1-w2x9"):
        _group(gate, name)
    (gate / "report.json").write_text('{"failed": true}\n')
    (gate / "notes").mkdir()
    (gate / "notes/plan.json").write_text("{}\n")
    done = _run(str(gate))
    assert done.returncode == 0
    assert _files(gate) == sorted(
        ["notes/plan.json", "report.json", *(f"{name}/{own}" for name in ("0-l6ouk117", "1-w2x9") for own in OWN)]
    )
    assert (gate / "report.json").read_text() == '{"failed": true}\n'
    assert not (gate / "0-l6ouk117/hooks").exists()
    assert not (gate / "0-l6ouk117/mutants/.pytest_cache").exists()
    assert done.stdout == (
        f"Mutation evidence: kept {2 * len(OWN)} files ({2 * _size(OWN)} bytes) and removed"
        f" {2 * len(WORKSPACE)} workspace files ({2 * _size(WORKSPACE)} bytes) in 2 group folders\n"
    )


def test_a_link_in_a_group_folder_is_removed_and_never_followed(tmp_path: Path) -> None:
    gate = tmp_path / ".mutation-gate"
    group = gate / "0-abc"
    (group / "killers").mkdir(parents=True)
    checkout = tmp_path / "checkout"
    (checkout / "hooks").mkdir(parents=True)
    (checkout / "hooks/sample.py").write_text("source\n")
    (group / "tests").symlink_to("../../checkout", target_is_directory=True)
    (group / "killers/7a1f").symlink_to("../../../checkout/hooks/sample.py")
    (group / "run.log").symlink_to("../../checkout/hooks/sample.py")
    (group / "results.json").symlink_to("../../missing.json")
    done = _run(str(gate))
    assert done.returncode == 0
    assert _files(gate) == []
    assert _files(checkout) == ["hooks/sample.py"]
    assert (checkout / "hooks/sample.py").read_text() == "source\n"
    assert done.stdout == (
        "Mutation evidence: kept 0 files (0 bytes) and removed 4 workspace files (95 bytes) in 1 group folders\n"
    )


def test_a_file_named_like_the_kill_memory_folder_is_workspace(tmp_path: Path) -> None:
    gate = tmp_path / ".mutation-gate"
    (gate / "0-abc").mkdir(parents=True)
    (gate / "0-abc/killers").write_text("not a folder\n")
    done = _run(str(gate))
    assert done.returncode == 0
    assert _files(gate) == []


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses file modes")
def test_a_folder_the_walk_cannot_read_fails_the_run(tmp_path: Path) -> None:
    gate = tmp_path / ".mutation-gate"
    _group(gate, "0-abc")
    locked = gate / "0-abc/docs"
    locked.chmod(0)
    try:
        done = _run(str(gate))
    finally:
        locked.chmod(0o700)
    assert done.returncode == 1
    assert "PermissionError" in done.stderr
    assert (locked / "results.json").is_file()


def test_a_missing_folder_keeps_and_removes_nothing(tmp_path: Path) -> None:
    done = _run(str(tmp_path / "missing"))
    assert done.returncode == 0
    assert done.stdout == (
        "Mutation evidence: kept 0 files (0 bytes) and removed 0 workspace files (0 bytes) in 0 group folders\n"
    )


@pytest.mark.parametrize("arguments", [[], ["first", "second"]])
def test_refuses_a_call_without_exactly_one_folder(tmp_path: Path, arguments: list[str]) -> None:
    done = _run(*arguments)
    assert done.returncode == 2
    assert done.stderr == "usage: mutation_evidence.py FOLDER\n"


def test_the_merged_verdict_is_the_same_from_the_kept_evidence(tmp_path: Path) -> None:
    shards = tmp_path / "shards"
    survivor = {
        "name": "hooks.sample.x_f__mutmut_1",
        "status": "survived",
        "lines": [2],
        "fingerprint": "abc",
        "diff": "-    return 1\n+    return 2",
    }
    for shard in range(2):
        folder = shards / f"mutation-report-{shard}"
        _group(folder, f"0-shard{shard}")
        counts = {"survived": 1} if shard == 0 else {"killed": 1}
        report = {
            "files": [
                {
                    "path": "hooks/sample.py",
                    "counts": counts,
                    "failures": [survivor] if shard == 0 else [],
                    "untouched_survivors": [],
                    "cleared": [],
                }
            ],
            "not_mutated": [],
            "failed": shard == 0,
            "shard": {"index": shard, "total": 2, "expected": 1, "graded": 1, "complete": True},
        }
        (folder / "report.json").write_text(json.dumps(report) + "\n")
    before = merge.main([str(shards), "--shards", "2", "--output", str(tmp_path / "before")])
    assert [_run(str(shards / f"mutation-report-{shard}")).returncode for shard in range(2)] == [0, 0]
    after = merge.main([str(shards), "--shards", "2", "--output", str(tmp_path / "after")])
    assert (before, after) == (1, 1)
    assert (tmp_path / "after/report.json").read_text() == (tmp_path / "before/report.json").read_text()
    assert json.loads((tmp_path / "after/report.json").read_text())["files"][0]["failures"] == [survivor]


@pytest.mark.parametrize(
    ("job", "upload", "guard"),
    [
        ("mutation-shard", "Upload complete mutation evidence", "needs.mutation-plan.outputs.forwarded != 'true'"),
        ("mutation-stats", "Upload complete inline mutation evidence", "steps.mutate.outcome != 'skipped'"),
        ("mutation-plan", "Upload complete inline mutation evidence", "steps.mutate.outcome != 'skipped'"),
    ],
)
def test_a_failed_shard_keeps_only_its_own_evidence_right_before_its_upload(job: str, upload: str, guard: str) -> None:
    steps = yaml.safe_load(WORKFLOW.read_text())["jobs"][job]["steps"]
    names = [step.get("name") for step in steps]
    keep = steps[names.index(KEEP)]
    assert keep["if"] == f"always() && job.status != 'success' && {guard}"
    assert keep["run"] == 'python -I "$RUNNER_TEMP/workflow/.github/scripts/mutation_evidence.py" .mutation-gate'
    assert names.index("Take the helper scripts from the workflow revision") < names.index(KEEP)
    assert names.index(KEEP) + 1 == names.index(upload)
    assert steps[names.index(upload)]["with"]["path"] == (
        "${{ job.status == 'success' && '.mutation-gate/report.json' || '.mutation-gate/' }}"
    )
