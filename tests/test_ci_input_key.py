import json
import subprocess
from pathlib import Path

import pytest
import yaml

from scripts import ci_input_key

_ROOT = Path(__file__).resolve().parents[1]
_KEY = "./.github/actions/input-key"
_SAVE = "./.github/actions/input-key/save"
_SKIP = "steps.key.outputs.hit != 'true'"
pytestmark = pytest.mark.unit


def _registry():
    return json.loads((_ROOT / ".github/input-keys.json").read_text())


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def _commit(repo, files):
    for name, text in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "c")
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path, monkeypatch):
    _git(tmp_path, "init", "-q")
    registry = {
        "job": {"paths": ["src", "tool.cfg"], "tools": ["python 3.12"]},
        "graded": {"paths": ["src"], "base_paths": ["budget.txt"], "tools": []},
    }
    _commit(
        tmp_path,
        {
            "scripts/ci_input_key.py": "keyer",
            ".github/actions/input-key/action.yml": "action",
            "registry.json": json.dumps(registry),
            "src/a.py": "a",
            "tool.cfg": "x",
            "docs/readme.md": "d",
            "budget.txt": "1",
        },
    )
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _key(repo, job="job", **kwargs):
    return ci_input_key.key(job, registry=repo / "registry.json", day="2026-10-16", **kwargs)


def test_an_unchanged_declared_input_keeps_the_key_and_an_undeclared_change_does_too(repo):
    before = _key(repo)
    assert before.startswith("input-pass-job-") and len(before) == len("input-pass-job-") + 64
    _commit(repo, {"docs/readme.md": "changed"})
    assert _key(repo) == before


@pytest.mark.parametrize(
    "change",
    [
        {"src/a.py": "changed"},
        {"src/new.py": "new"},
        {"tool.cfg": "y"},
        {"scripts/ci_input_key.py": "keyer 2"},
        {".github/actions/input-key/action.yml": "action 2"},
    ],
)
def test_a_change_to_a_declared_input_or_the_keyer_changes_the_key(repo, change):
    before = _key(repo)
    _commit(repo, change)
    assert _key(repo) != before


def test_the_job_tools_and_day_are_part_of_the_key(repo):
    before = _key(repo)
    registry = json.loads((repo / "registry.json").read_text())
    registry["job"]["tools"] = ["python 3.13"]
    (repo / "registry.json").write_text(json.dumps(registry))
    assert _key(repo) != before
    assert ci_input_key.key("job", registry=repo / "registry.json", day="2026-10-17") != _key(repo)
    registry["other"] = registry["job"]
    (repo / "registry.json").write_text(json.dumps(registry))
    assert _key(repo, "other").removeprefix("input-pass-other-") != _key(repo).removeprefix("input-pass-job-")


def test_the_key_reads_the_head_it_is_given_and_base_paths_at_the_base(repo):
    first = _git(repo, "rev-parse", "HEAD")
    graded = _key(repo, "graded", base=first)
    second = _commit(repo, {"budget.txt": "2"})
    assert _key(repo, "graded", base=first) == graded
    assert _key(repo, "graded", base=second) != graded
    _commit(repo, {"src/a.py": "changed"})
    assert _key(repo, "job", head=first) == _key(repo, "job", head=second)
    assert _key(repo, "job", head=first) != _key(repo, "job")


def test_a_missing_declared_input_or_base_cannot_key(repo):
    registry = json.loads((repo / "registry.json").read_text())
    registry["broken"] = {"paths": ["nowhere"], "tools": []}
    (repo / "registry.json").write_text(json.dumps(registry))
    with pytest.raises(SystemExit, match="declared input nowhere is missing at HEAD"):
        _key(repo, "broken")
    with pytest.raises(SystemExit, match="graded declares base paths and needs --base"):
        _key(repo, "graded")
    with pytest.raises(SystemExit, match="declared input budget.txt is missing at nosuchref"):
        _key(repo, "graded", base="nosuchref")


def test_main_writes_the_key_to_the_step_output(repo, monkeypatch, capsys):
    (repo / ".github/input-keys.json").write_text((repo / "registry.json").read_text())
    output = repo / "output.txt"
    output.write_text("before=1\n")
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    assert ci_input_key.main(["--job", "job"]) == 0
    value = ci_input_key.key("job", registry=repo / ".github/input-keys.json")
    assert capsys.readouterr().out == f"key={value}\n"
    assert output.read_text() == f"before=1\nkey={value}\n"


def test_every_declared_input_exists_and_covers_its_own_workflow():
    for job, entry in _registry().items():
        assert set(entry) <= {"paths", "base_paths", "tools"}
        assert entry["tools"]
        assert f".github/workflows/{job}.yml" in entry["paths"]
        for path in entry["paths"] + entry.get("base_paths", []):
            assert (_ROOT / path).exists(), path


def test_the_lookup_never_restores_and_reports_a_hit_as_true_or_false():
    steps = yaml.safe_load((_ROOT / ".github/actions/input-key/action.yml").read_text())["runs"]["steps"]
    key, lookup, notice = steps
    assert key["run"] == 'python3 -I scripts/ci_input_key.py --job "$JOB" --base "$BASE"'
    assert lookup["uses"] == "actions/cache/restore@v4"
    assert lookup["with"]["lookup-only"] is True
    assert lookup["with"]["key"] == "${{ steps.key.outputs.key }}"
    assert notice["if"] == "steps.lookup.outputs.cache-hit == 'true'"
    outputs = yaml.safe_load((_ROOT / ".github/actions/input-key/action.yml").read_text())["outputs"]
    assert outputs["hit"]["value"] == "${{ steps.lookup.outputs.cache-hit == 'true' }}"
    save = yaml.safe_load((_ROOT / ".github/actions/input-key/save/action.yml").read_text())["runs"]["steps"]
    assert save[-1]["uses"] == "actions/cache/save@v4"
    assert save[-1]["with"]["key"] == "${{ inputs.key }}"


@pytest.mark.parametrize("job", sorted(_registry()))
def test_a_keyed_job_runs_its_heavy_steps_only_on_a_miss_and_saves_only_after_they_pass(job):
    jobs = yaml.safe_load((_ROOT / f".github/workflows/{job}.yml").read_text())["jobs"]
    [steps] = [job_def["steps"] for job_def in jobs.values() if any(s.get("uses") == _KEY for s in job_def["steps"])]
    index = next(i for i, step in enumerate(steps) if step.get("uses") == _KEY)
    assert steps[index]["id"] == "key"
    assert steps[index]["with"]["job"] == job
    assert all("if" not in step for step in steps[:index])
    [save] = [step for step in steps if step.get("uses") == _SAVE]
    assert save["if"] == "steps.key.outputs.hit == 'false'"
    assert save["with"] == {"key": "${{ steps.key.outputs.key }}"}
    heavy = [step for step in steps[index + 1 :] if step is not save]
    assert steps.index(save) > max(i for i, step in enumerate(steps) if _SKIP in step.get("if", ""))
    assert heavy
    for step in heavy:
        assert _SKIP in step.get("if", "") or step.get("if") in {"inputs.streams", "${{ always() && inputs.streams }}"}
