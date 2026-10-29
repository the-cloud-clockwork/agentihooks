import hashlib
import json
import re
import subprocess
from datetime import datetime, timedelta, timezone
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


def _key(repo, job="job", base="", day="2026-10-16", **kwargs):
    return ci_input_key.key(job, base, day, registry=repo / "registry.json", **kwargs)


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
    assert _key(repo, day="2026-10-17") != _key(repo)
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


def test_the_key_is_the_digest_of_every_declared_input(repo):
    head = _git(repo, "rev-parse", "HEAD")
    record = {
        "version": 1,
        "job": "graded",
        "entry": {"paths": ["src"], "base_paths": ["budget.txt"], "tools": []},
        "keyer": [
            _git(repo, "rev-parse", "HEAD:scripts/ci_input_key.py"),
            _git(repo, "rev-parse", "HEAD:.github/actions/input-key"),
        ],
        "paths": {"src": _git(repo, "rev-parse", "HEAD:src")},
        "base_paths": {"budget.txt": _git(repo, "rev-parse", f"{head}:budget.txt")},
        "environment": ["a=1", "b=2"],
        "day": "2026-10-16",
    }
    digest = hashlib.sha256(json.dumps(record, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    assert _key(repo, "graded", base=head, environment=("b=2", "a=1")) == f"input-pass-graded-{digest}"


def test_an_undeclared_job_cannot_key(repo):
    with pytest.raises(SystemExit, match="nobody declares no inputs in"):
        _key(repo, "nobody")


class _Clock:
    @staticmethod
    def now(zone):
        return datetime(2026, 10, 16, 23, 30, tzinfo=timezone.utc).astimezone(zone or timezone(timedelta(hours=2)))


def test_main_keys_the_runner_the_utc_day_and_the_given_base_and_environment(repo, monkeypatch, capsys):
    registry = repo / ".github/input-keys.json"
    registry.write_text((repo / "registry.json").read_text())
    base = _git(repo, "rev-parse", "HEAD")
    _commit(repo, {"budget.txt": "2"})
    monkeypatch.setattr(ci_input_key, "datetime", _Clock)
    runner = {"RUNNER_OS": "Linux", "RUNNER_ARCH": "X64", "RUNNER_ENVIRONMENT": "self-hosted", "ImageVersion": "7"}
    for name, value in runner.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("ImageOS", raising=False)
    output = repo / "output.txt"
    output.write_text("before=1\n")
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    args = ["--job", "graded", "--base", base, "--environment", "redis=sha256:1"]
    assert ci_input_key.main(args) == 0
    environment = ("redis=sha256:1", "ImageOS=", *(f"{name}={value}" for name, value in runner.items()))
    expected = ci_input_key.key("graded", base, "2026-10-16", registry=registry, environment=environment)
    assert capsys.readouterr().out == f"key={expected}\n"
    assert output.read_text() == f"before=1\nkey={expected}\n"
    assert ci_input_key.main([*args[:3], "HEAD", *args[4:]]) == 0
    assert capsys.readouterr().out != f"key={expected}\n"
    monkeypatch.setenv("ImageVersion", "8")
    assert ci_input_key.main(args) == 0
    assert capsys.readouterr().out != f"key={expected}\n"
    monkeypatch.delenv("GITHUB_OUTPUT")
    assert ci_input_key.main(["--job", "job", "--base", ""]) == 0
    plain = ci_input_key.key(
        "job", "", "2026-10-16", registry=registry, environment=environment[1:5] + ("ImageVersion=8",)
    )
    assert capsys.readouterr().out == f"key={plain}\n"


@pytest.mark.parametrize("args", [["--base", ""], ["--job", "job"]])
def test_main_needs_the_job_and_an_explicit_base(repo, args):
    with pytest.raises(SystemExit) as exit_info:
        ci_input_key.main(args)
    assert exit_info.value.code == 2


def test_every_declared_input_exists_and_covers_its_own_workflow():
    for job, entry in _registry().items():
        assert set(entry) <= {"paths", "base_paths", "tools"}
        assert entry["tools"]
        assert f".github/workflows/{job}.yml" in entry["paths"]
        for path in entry["paths"] + entry.get("base_paths", []):
            assert (_ROOT / path).exists(), path


def test_the_chart_proof_key_covers_every_test_file_its_scripts_run():
    declared = _registry()["helm-kind"]["paths"]
    scripts = sorted((_ROOT / "deploy/helm/agentihooks-swarm/ci").glob("*.sh"))
    roots = r"(?:scripts|hooks|profiles|docker|docs|media|tests)/[\w./-]+"
    used = {path for script in scripts for path in re.findall(roots, script.read_text())}
    assert {
        "tests/chart_hub.py",
        "tests/chart_nats.py",
        "tests/chart_hive_only.py",
        "tests/chart_github_auth.py",
    } <= used
    pending = [path for path in used if path.startswith("tests/") and path.endswith(".py")]
    while pending:
        source = (_ROOT / pending.pop()).read_text()
        for name in re.findall(r"^(?:from|import) (chart_\w+)", source, re.MULTILINE):
            if f"tests/{name}.py" not in used:
                used.add(f"tests/{name}.py")
                pending.append(f"tests/{name}.py")
    for path in used:
        assert any(path == entry or path.startswith(f"{entry}/") for entry in declared), path


def test_the_lookup_only_checks_the_key_and_reports_a_hit_as_true_or_false():
    steps = yaml.safe_load((_ROOT / ".github/actions/input-key/action.yml").read_text())["runs"]["steps"]
    key, lookup, notice = steps
    assert key["run"] == 'python3 -I scripts/ci_input_key.py --job "$JOB" --base "$BASE" --environment "$ENVIRONMENT"'
    assert key["env"]["ENVIRONMENT"] == "${{ inputs.environment }}"
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
    [(name, keyed)] = [
        (name, job_def) for name, job_def in jobs.items() if any(s.get("uses") == _KEY for s in job_def["steps"])
    ]
    steps = keyed["steps"]
    index = next(i for i, step in enumerate(steps) if step.get("uses") == _KEY)
    assert steps[index]["id"] == "key"
    assert steps[index]["with"]["job"] == job
    assert all(
        step.get("if") in {None, "${{ !inputs.streams }}", "${{ !inputs.streams && !inputs.compare }}"}
        for step in steps[:index]
    )
    if "hit" in keyed.get("outputs", {}):
        assert keyed["outputs"]["hit"] == "${{ steps.key.outputs.hit }}"
        assert keyed["outputs"]["key"] == "${{ steps.key.outputs.key }}"
        [scenario] = [definition for definition in jobs.values() if definition.get("needs") == [name]]
        assert "services" not in keyed
        assert scenario["if"] == f"github.event_name == 'workflow_dispatch' || needs.{name}.outputs.hit != 'true'"
        [save] = [step for step in scenario["steps"] if step.get("uses") == _SAVE]
        assert scenario["steps"][-1] is save
        assert save["if"] == "github.event_name == 'push' || github.event_name == 'workflow_dispatch'"
        assert save["with"] == {"key": f"${{{{ needs.{name}.outputs.key }}}}"}
        assert len(scenario["steps"]) > 1
        return
    [save] = [step for step in steps if step.get("uses") == _SAVE]
    assert save["if"] == (
        "steps.key.outputs.hit == 'false' && (github.event_name == 'push' || github.event_name == 'workflow_dispatch')"
    )
    assert save["with"] == {"key": "${{ steps.key.outputs.key }}"}
    heavy = [step for step in steps[index + 1 :] if step is not save]
    assert steps.index(save) > max(i for i, step in enumerate(steps) if _SKIP in step.get("if", ""))
    assert heavy
    for step in heavy:
        assert _SKIP in step.get("if", "") or step.get("if") in {
            "inputs.streams",
            "${{ always() && inputs.streams }}",
            "inputs.compare",
            "${{ always() && inputs.compare }}",
        }
