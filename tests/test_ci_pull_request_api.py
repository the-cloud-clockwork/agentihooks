import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]
API_CALL = re.compile(r"\bgh\b(?!-)|api\.github\.com|GITHUB_API_URL")
CALL = re.compile(
    r"(?:^|[\s;&|(])(?:bash|sh|python3?)\s+\"?(?P<path>[\w./-]+\.(?:sh|py))"
    r"|(?:^|[\s;&|(])\./(?P<exe>[\w./-]+)"
    r"|\bpython3?\s+-m\s+(?P<module>[\w.]+)"
    r"|(?:\$\(dirname \"\$0\"\)|\$GITHUB_ACTION_PATH)\"?/(?P<sibling>[\w.-]+\.(?:sh|py))"
)
# The documented local `--ci` download path; the workflow passes `--samples`, so CI never reaches it.
LOCAL_ONLY = {"tests/refresh_durations.py": 3}
TOKEN = re.compile(r"github\.token|secrets\.(github|gh)_\w*", re.IGNORECASE)
APP_TOKEN = "${{ steps.app-token.outputs.token }}"


def _workflow(name: str) -> dict:
    return yaml.safe_load((ROOT / ".github/workflows" / name).read_text())


def _jobs() -> dict:
    return _workflow("test.yml")["jobs"]


def _all_jobs():
    for name, job in _jobs().items():
        called = job.get("uses", "")
        if called.startswith("./.github/workflows/"):
            for inner, inner_job in _workflow(Path(called).name)["jobs"].items():
                yield f"{name}/{inner}", inner_job
        else:
            yield name, job


def _all_steps():
    for name, job in _all_jobs():
        if TOKEN.search(str(job.get("env", {}))):
            yield name, {"name": "job env", "env": job["env"]}
        for step in job.get("steps", []):
            yield name, step


def _holds_token(step: dict) -> bool:
    return bool(
        TOKEN.search(str(step.get("env", {})))
        or TOKEN.search(str(step.get("with", {})))
        or TOKEN.search(step.get("run", ""))
        or step.get("uses", "").startswith("actions/github-script@")
    )


def _offends(step: dict) -> bool:
    on_app_quota = (step.get("env") or {}).get("GH_TOKEN") == APP_TOKEN
    return _holds_token(step) or (bool(API_CALL.search(step.get("run", ""))) and not on_app_quota)


def test_no_step_on_any_event_holds_the_workflow_token_or_calls_the_api():
    offenders = [f"{job}: {step.get('name') or step.get('uses')}" for job, step in _all_steps() if _offends(step)]
    assert offenders == []


def _step_runs():
    for _, step in _all_steps():
        action = step.get("uses", "")
        if action.startswith("./.github/actions/"):
            folder = ROOT / action
            for inner in yaml.safe_load((folder / "action.yml").read_text())["runs"]["steps"]:
                yield inner.get("run", ""), folder
        yield step.get("run", ""), ROOT


def _called(text: str, here: Path):
    for match in CALL.finditer(text):
        if match["module"]:
            base = ROOT / Path(*match["module"].split("."))
            candidates = [base.with_suffix(".py"), base / "__main__.py"]
        elif match["sibling"]:
            candidates = [here / match["sibling"]]
        else:
            candidates = [ROOT / (match["path"] or match["exe"])]
        yield from (path.resolve() for path in candidates if path.is_file())


def _api_calls(runs) -> dict[Path, int]:
    pending = [path for run, here in runs for path in _called(run, here)]
    seen = set()
    while pending:
        path = pending.pop()
        if path not in seen:
            seen.add(path)
            pending.extend(_called(path.read_text(), path.parent))
    return {path: len(API_CALL.findall(path.read_text())) for path in seen}


def test_no_script_a_step_runs_calls_the_api():
    calls = {str(path.relative_to(ROOT)): count for path, count in _api_calls(_step_runs()).items()}
    assert {
        ".github/actions/browser-cache/verify.sh",
        ".github/coverage/combine.sh",
        "scripts/ci_mutation/__main__.py",
        "scripts/brain-smoke",
        "scripts/packaging/swarm-smoke.sh",
    } | LOCAL_ONLY.keys() <= calls.keys()
    assert {script: count for script, count in calls.items() if count != LOCAL_ONLY.get(script, 0)} == {}


@pytest.mark.parametrize(
    ("call", "script"),
    [
        ("bash .github/coverage/combine.sh --downloaded 8 || status=$?", ".github/coverage/combine.sh"),
        ("timeout 60s python -m tests.shard_check --shards 8", "tests/shard_check.py"),
        ("python -m scripts.ci_mutation report", "scripts/ci_mutation/__main__.py"),
        ("./scripts/brain-smoke --no-color", "scripts/brain-smoke"),
    ],
    ids=["bash-path", "python-module", "python-package", "executable"],
)
def test_each_way_a_step_runs_a_script_is_read(call, script):
    assert list(_called(call, ROOT)) == [ROOT / script]


@pytest.mark.parametrize(
    "call",
    ['python "$(dirname "$0")/helper.py" 42 8', 'timeout 10s bash "$GITHUB_ACTION_PATH/helper.py"'],
    ids=["script-sibling", "action-sibling"],
)
def test_a_script_run_beside_its_caller_is_read(tmp_path, call):
    (tmp_path / "helper.py").write_text("")
    assert list(_called(call, tmp_path)) == [(tmp_path / "helper.py").resolve()]


@pytest.mark.parametrize(
    "plant",
    [
        "gh api rate_limit",
        'curl "$GITHUB_API_URL/rate_limit"',
        'urlopen("https://api.github.com/rate_limit")',
        "gh run download 42 --dir out",
    ],
    ids=["gh-api", "api-url", "api-host", "gh-run-download"],
)
@pytest.mark.parametrize("planted", ["outer.sh", "inner.py"])
def test_an_api_call_planted_in_a_called_script_is_counted(tmp_path, plant, planted):
    (tmp_path / "outer.sh").write_text('python "$(dirname "$0")/inner.py" 8\n')
    (tmp_path / "inner.py").write_text("print('merged')\n")
    with (tmp_path / planted).open("a") as script:
        script.write(f"{plant}\n")
    calls = _api_calls([('timeout 60s bash "$GITHUB_ACTION_PATH/outer.sh"', tmp_path)])
    assert calls == {(tmp_path / name).resolve(): int(name == planted) for name in ("outer.sh", "inner.py")}


@pytest.mark.parametrize(
    ("step", "offends"),
    [
        ({"env": {"GH_TOKEN": APP_TOKEN}, "run": 'gh api "repos/$GITHUB_REPOSITORY/actions/runs"'}, False),
        ({"env": {"GH_TOKEN": APP_TOKEN, "BASE_SHA": "x"}, "run": "gh api rate_limit"}, False),
        ({"env": {"GH_TOKEN": APP_TOKEN, "OTHER": "${{ github.token }}"}, "run": "gh api rate_limit"}, True),
        ({"env": {"GH_TOKEN": "${{ github.token }}"}, "run": "gh api rate_limit"}, True),
        ({"env": {"GH_TOKEN": APP_TOKEN}, "run": "echo ${{ github.token }}"}, True),
    ],
    ids=[
        "app-token",
        "app-token-with-extra-env",
        "app-token-with-workflow-token-env",
        "workflow-token",
        "app-token-beside-workflow-token",
    ],
)
def test_only_an_api_step_on_the_app_token_alone_stays_off_the_shared_quota(step, offends):
    assert _offends(step) is offends


@pytest.mark.parametrize(
    "plant",
    [
        {"run": "gh --repo o/r api rate_limit"},
        {"run": 'curl "$GITHUB_API_URL/rate_limit"'},
        {"env": {"GITHUB_TOKEN": "${{ secrets.GITHUB_TOKEN }}"}},
        {"uses": "actions/github-script@v7", "with": {"github-token": "${{ github.token }}"}},
        {"run": "echo ${{ github.token }}"},
        {"uses": "actions/github-script@v7"},
        {"env": {"TOKEN": "${{ secrets.GH_PAT }}"}},
        {"run": 'gh api "repos/$GITHUB_REPOSITORY/actions/artifacts"'},
        {"run": "gh run download 42 --dir out"},
        {"run": "curl https://api.github.com/rate_limit"},
        {"uses": "actions/download-artifact@v4", "with": {"github-token": "${{ secrets.GITHUB_TOKEN }}"}},
    ],
    ids=[
        "gh-with-flags",
        "api-url",
        "github-token-env",
        "action-input",
        "token-in-run",
        "script-default",
        "pat",
        "gh-api",
        "gh-run-download",
        "api-host",
        "cross-run-download",
    ],
)
def test_each_way_of_reaching_the_api_is_an_offender(plant):
    assert _holds_token(plant) or API_CALL.search(plant.get("run", ""))


def test_sonar_downloads_this_runs_coverage_after_the_shards():
    sonar = _jobs()["sonar"]
    assert sonar["needs"] == ["unit"]
    steps = sonar["steps"]
    download = next(step for step in steps if step.get("name") == "Download shard coverage")
    merge = next(step for step in steps if step.get("name") == "Merge shard coverage")
    assert download["if"] == merge["if"] == "steps.current.outputs.superseded != 'true'"
    assert download["uses"].startswith("actions/download-artifact@")
    assert download["with"] == {"pattern": "coverage-3.12-*", "path": ".coverage-shards"}
    assert steps.index(download) < steps.index(merge)
    assert "env" not in merge
    assert "bash .github/coverage/combine.sh --downloaded 8 ||" in merge["run"]


@pytest.mark.parametrize("job", ["unit", "shard-check", "test-count", "size", "lint"])
def test_a_dev_push_runs_every_step_of_the_job_itself(job):
    steps = _jobs()[job]["steps"]
    assert steps[0]["uses"] == "actions/checkout@v4"
    assert "if" not in steps[0]
    assert [step.get("name") for step in steps if "skip" in step.get("if", "")] == []


@pytest.mark.parametrize("job", sorted(_jobs()))
def test_no_job_is_granted_the_actions_api(job):
    assert "actions" not in (_jobs()[job].get("permissions") or {})


def test_no_job_skips_on_a_tree_another_run_passed():
    jobs = _jobs()
    assert "record-pass" not in jobs
    assert "outputs" not in jobs["lint"]
    assert "steps.lookup" not in (ROOT / ".github/workflows/test.yml").read_text()


def _superseded(tmp_path, dev_head: str, sha: str = "a" * 40) -> str | None:
    step = next(step for step in _jobs()["sonar"]["steps"] if step.get("id") == "current")
    tools = tmp_path / "bin"
    tools.mkdir()
    listing = f'echo "{dev_head}\trefs/heads/dev"' if dev_head else "true"
    (tools / "git").write_text(f'#!/usr/bin/env bash\n[[ "$*" == "ls-remote origin refs/heads/dev" ]]\n{listing}\n')
    (tools / "git").chmod(0o755)
    output = tmp_path / "output"
    result = subprocess.run(
        ["bash", "-e", "-c", step["run"]],
        env={**os.environ, "PATH": f"{tools}:{os.environ['PATH']}", "GITHUB_OUTPUT": str(output), "SHA": sha},
        check=False,
    )
    return output.read_text() if result.returncode == 0 else None


@pytest.mark.parametrize(("dev_head", "superseded"), [("a" * 40, "false"), ("b" * 40, "true")])
def test_a_dev_push_skips_the_analysis_once_dev_moved_past_it(tmp_path, dev_head, superseded):
    assert _superseded(tmp_path, dev_head) == f"superseded={superseded}\n"


def test_an_unreadable_dev_head_fails_the_check_instead_of_skipping_the_analysis(tmp_path):
    assert _superseded(tmp_path, "") is None


def test_sonar_names_the_current_dev_head_from_git_and_gates_the_scan_on_it():
    steps = _jobs()["sonar"]["steps"]
    current = next(step for step in steps if step.get("id") == "current")
    assert current["if"] == "github.event_name == 'push'"
    assert current["env"] == {"SHA": "${{ github.sha }}"}
    names = [step.get("name") for step in steps]
    assert "Wait for older dev analyses" not in names
    later = steps[steps.index(current) + 1 :]
    gated = {
        "Download shard coverage",
        "Merge shard coverage",
        "SonarQube Scan",
        "SonarQube Quality Gate",
        "Hold the Delivery L2 conditions",
    }
    assert {
        step["name"] for step in later if "steps.current.outputs.superseded != 'true'" in step.get("if", "")
    } >= gated
