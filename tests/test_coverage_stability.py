from pathlib import Path

import pytest
import yaml
from coverage import CoverageData

from tests import coverage_stability as stability

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).parent.parent
REPO = "owner/repo"


@pytest.fixture(autouse=True)
def _repository(monkeypatch):
    monkeypatch.setenv("GITHUB_REPOSITORY", REPO)


def _shards(root: Path, run: str, lines: dict[str, set[int]]) -> list[Path]:
    paths = []
    for n in range(1, stability.SHARDS + 1):
        path = root / run / f"coverage-3.12-{n}" / ".coverage"
        path.parent.mkdir(parents=True)
        data = CoverageData(basename=str(path))
        data.add_lines(lines if n == 1 else {"hooks/always.py": {1}})
        data.write()
        paths.append(path)
    return paths


class _GitHub:
    def __init__(self, root: Path, runs: dict[str, tuple[str, dict[str, set[int]]]], dev: list[str] | None = None):
        self.root = root
        self.runs = runs
        self.dev = dev or []
        self.kept = {run: stability.SHARDS for run in runs}
        self.calls = []

    def __call__(self, *args: str) -> str:
        self.calls.append(args)
        if args[:2] == ("run", "download"):
            dest = Path(args[args.index("--dir") + 1])
            _shards(dest.parent, dest.name, self.runs[args[2]][1])
            return ""
        path = args[1]
        if path.startswith(f"repos/{REPO}/commits?sha=dev"):
            return "\n".join(self.dev)
        if "/actions/workflows/test.yml/runs?head_sha=" in path:
            sha = path.split("head_sha=")[1].split("&")[0]
            return "\n".join(run for run, (commit, _) in self.runs.items() if commit == sha)
        run = path.split("/actions/runs/")[1].split("/")[0]
        if path.endswith("/artifacts?per_page=100"):
            return "\n".join(f"coverage-3.12-{n}" for n in range(1, self.kept[run] + 1)) + "\nsonar-report"
        return self.runs[run][0]


def test_differences_name_every_line_some_runs_ran_and_others_missed():
    found = stability.differences(
        {
            "1": {"hooks/a.py": [1, 2, 3], "hooks/b.py": [5]},
            "2": {"hooks/a.py": [1, 3]},
            "3": {"hooks/a.py": [1, 2, 3], "hooks/b.py": [5]},
        }
    )
    assert found == [("hooks/a.py", 2, ["1", "3"], ["2"]), ("hooks/b.py", 5, ["1", "3"], ["2"])]


def test_identical_runs_have_no_differences():
    assert stability.differences({"1": {"hooks/a.py": [1]}, "2": {"hooks/a.py": [1]}}) == []


def test_report_lists_module_line_and_the_runs_on_each_side():
    text = stability.report(["1", "2"], [("hooks/a.py", 2, ["1"], ["2"])])
    assert "hooks/a.py:2 ran in 1, missed in 2" in text
    assert "1 measured line differs across runs 1, 2" in text
    assert stability.report(["1", "2"], []) == "Every measured line ran the same way across runs 1, 2"


def test_runs_compares_the_raw_shard_databases_of_one_commit(tmp_path, monkeypatch, capsys):
    github = _GitHub(
        tmp_path,
        {"11": ("abc", {"hooks/amygdala_hook.py": {42, 75}}), "12": ("abc", {"hooks/amygdala_hook.py": {75}})},
    )
    monkeypatch.setattr(stability, "_gh", github)
    assert stability.main(["--work", str(tmp_path / "work"), "runs", "11", "12"]) == 1
    out = capsys.readouterr().out
    assert "hooks/amygdala_hook.py:42 ran in 11, missed in 12" in out
    assert "hooks/amygdala_hook.py:75" not in out
    downloads = [call for call in github.calls if call[:2] == ("run", "download")]
    names = [f"coverage-3.12-{n}" for n in range(1, stability.SHARDS + 1)]
    for call in downloads:
        assert call[call.index("--repo") + 1] == REPO
        assert [value for flag, value in zip(call, call[1:]) if flag == "--name"] == names
    assert len(downloads) == 2


def test_runs_passes_when_every_line_ran_the_same_way(tmp_path, monkeypatch, capsys):
    same = {"hooks/a.py": {1, 2}}
    monkeypatch.setattr(stability, "_gh", _GitHub(tmp_path, {"11": ("abc", same), "12": ("abc", same)}))
    assert stability.main(["--work", str(tmp_path / "work"), "runs", "11", "12"]) == 0
    assert "Every measured line ran the same way across runs 11, 12" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("runs", "ids", "message"),
    [
        ({"11": ("abc", {}), "12": ("def", {})}, ["11", "12"], "different commits"),
        ({"11": ("abc", {})}, ["11"], "two or more distinct runs"),
        ({"11": ("abc", {})}, ["11", "11"], "two or more distinct runs"),
    ],
)
def test_runs_that_cannot_be_compared_are_red(tmp_path, monkeypatch, capsys, runs, ids, message):
    monkeypatch.setattr(stability, "_gh", _GitHub(tmp_path, runs))
    assert stability.main(["--work", str(tmp_path / "work"), "runs", *ids]) == 1
    assert message in capsys.readouterr().out


def test_a_run_missing_a_shard_is_red(tmp_path, monkeypatch, capsys):
    github = _GitHub(tmp_path, {"11": ("abc", {"hooks/a.py": {1}}), "12": ("abc", {"hooks/a.py": {1}})})
    github.kept["12"] = stability.SHARDS - 1
    monkeypatch.setattr(stability, "_gh", github)
    assert stability.main(["--work", str(tmp_path / "work"), "runs", "11", "12"]) == 1
    assert f"run 12 kept {stability.SHARDS - 1} of {stability.SHARDS} coverage shards" in capsys.readouterr().out


def test_latest_compares_the_newest_dev_commit_with_two_complete_runs(tmp_path, monkeypatch, capsys):
    github = _GitHub(
        tmp_path,
        {
            "30": ("new", {"hooks/a.py": {1}}),
            "21": ("mid", {"hooks/a.py": {1}}),
            "22": ("mid", {"hooks/a.py": {1}}),
            "23": ("mid", {"hooks/a.py": {1, 2}}),
            "11": ("old", {"hooks/a.py": {1}}),
            "12": ("old", {"hooks/a.py": {1}}),
        },
        dev=["new", "mid", "old"],
    )
    github.kept["22"] = 0
    monkeypatch.setattr(stability, "_gh", github)
    assert stability.main(["--work", str(tmp_path / "work"), "latest"]) == 1
    out = capsys.readouterr().out
    assert "hooks/a.py:2 ran in 23, missed in 21" in out


def test_latest_without_two_complete_runs_is_red(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(stability, "_gh", _GitHub(tmp_path, {"30": ("new", {})}, dev=["new"]))
    assert stability.main(["--work", str(tmp_path / "work"), "latest", "--depth", "5"]) == 1
    assert "no dev commit among the newest 5" in capsys.readouterr().out


def _workflow() -> dict:
    return yaml.safe_load((_ROOT / ".github/workflows/coverage-stability.yml").read_text())


def test_the_scheduled_job_compares_the_newest_dev_runs_and_fails_on_any_difference():
    workflow = _workflow()
    triggers = workflow[True]
    assert triggers["schedule"]
    assert "workflow_dispatch" in triggers
    assert "pull_request" not in triggers
    assert triggers["push"]["branches"] == ["dev"]
    assert sorted(triggers["push"]["paths"]) == [
        ".github/workflows/coverage-stability.yml",
        "tests/coverage_grade.py",
        "tests/coverage_stability.py",
    ]
    (job,) = workflow["jobs"].values()
    assert job["permissions"] == {"actions": "read", "contents": "read"}
    (compare,) = [step for step in job["steps"] if step.get("id") == "compare"]
    assert compare["env"]["GH_TOKEN"] == "${{ github.token }}"
    assert 'python -m tests.coverage_stability --work "$RUNNER_TEMP/coverage-stability" latest' in compare["run"]
    assert "set -o pipefail" in compare["run"]
    assert all("continue-on-error" not in step and "if" not in step for step in job["steps"])
