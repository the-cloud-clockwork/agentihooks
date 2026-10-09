import ast
import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]
API_CALL = re.compile(r"\b_?gh\b(?!-)|api\.github\.com|GITHUB_API_URL")
MODULE = re.compile(r"\bpython3?\b[^\n;&|]*?\s-m\s+([\w.]+)")
BESIDE = re.compile(r"\$\(dirname \"\$0\"\)|\$\{?GITHUB_ACTION_PATH\}?")
WORKSPACE = re.compile(r"\$\{?GITHUB_WORKSPACE\}?")
# The documented local `--ci` download; the workflow passes `--samples` with `--ci 5`, so CI never calls these.
LOCAL_ONLY = {ROOT / "tests/refresh_durations.py": {"_gh", "ci_run_ids", "ci_download"}}
TOKEN = re.compile(
    r"github\s*(\.\s*token|\[\s*['\"]token['\"]\s*\])"
    r"|secrets\s*(\.|\[\s*['\"])\s*(github|gh)_\w*"
    r"|\$\{\{(?:(?!\}\})[\s\S])*?(?<![\w.'\"-])(secrets|github)\s*(\)|\}\})",
    re.IGNORECASE,
)
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
        if TOKEN.search(_values(job.get("env"))):
            yield name, {"name": "job env", "env": job["env"]}, ROOT
        for step in job.get("steps", []):
            action = step.get("uses", "")
            if action.startswith("./.github/actions/"):
                folder = ROOT / action
                for inner in yaml.safe_load((folder / "action.yml").read_text())["runs"]["steps"]:
                    yield f"{name}/{folder.name}", inner, folder
            yield name, step, ROOT


def _values(mapping: dict | None) -> str:
    return "\n".join(map(str, (mapping or {}).values()))


def _holds_token(step: dict) -> bool:
    return bool(
        TOKEN.search(_values(step.get("env")))
        or TOKEN.search(_values(step.get("with")))
        or TOKEN.search(step.get("run", ""))
        or step.get("uses", "").startswith("actions/github-script@")
    )


def _offends(step: dict) -> bool:
    on_app_quota = (step.get("env") or {}).get("GH_TOKEN") == APP_TOKEN
    return _holds_token(step) or (bool(API_CALL.search(step.get("run", ""))) and not on_app_quota)


def test_no_step_on_any_event_holds_the_workflow_token_or_calls_the_api():
    offenders = [f"{job}: {step.get('name') or step.get('uses')}" for job, step, _ in _all_steps() if _offends(step)]
    assert offenders == []


def _called(text: str, here: Path):
    text = WORKSPACE.sub(str(ROOT), BESIDE.sub(str(here), text))
    for module in MODULE.findall(text):
        base = ROOT / Path(*module.split("."))
        yield from (path.resolve() for path in (base.with_suffix(".py"), base / "__main__.py") if path.is_file())
    for token in re.findall(r"[\w./-]+", text):
        path = (ROOT / token).resolve()
        inside = path.is_relative_to(ROOT) or path.is_relative_to(here.resolve())
        if inside and path.is_file() and (path.suffix in {".sh", ".py"} or os.access(path, os.X_OK)):
            yield path


def _api_calls(runs) -> dict[Path, int]:
    pending = [path for run, here in runs for path in _called(run, here)]
    seen = set()
    while pending:
        path = pending.pop()
        if path not in seen:
            seen.add(path)
            pending.extend(_called(path.read_text(), path.parent))
    return {path: len(API_CALL.findall(_reached_source(path))) for path in seen}


def _reached_source(path: Path) -> str:
    lines = path.read_text().splitlines()
    for node in ast.parse("\n".join(lines)).body if path in LOCAL_ONLY else []:
        if isinstance(node, ast.FunctionDef) and node.name in LOCAL_ONLY[path]:
            lines[node.lineno - 1 : node.end_lineno] = [""] * (node.end_lineno - node.lineno + 1)
    return "\n".join(lines)


def test_no_script_a_step_runs_calls_the_api():
    calls = _api_calls((step.get("run", ""), here) for _, step, here in _all_steps())
    assert sorted(str(path.relative_to(ROOT)) for path in calls) == [
        ".github/actions/browser-cache/select-artifacts.sh",
        ".github/actions/browser-cache/verify.sh",
        ".github/coverage/combine.sh",
        ".github/coverage/proxy.py",
        "deploy/helm/agentihooks-swarm/ci/kind-due.sh",
        "deploy/helm/agentihooks-swarm/ci/kind-smoke.sh",
        "docker/swarm-node/smoke.sh",
        "hooks/__main__.py",
        "hooks/hook_manager.py",
        "hooks/targets/normalizer.py",
        "scripts/brain-smoke",
        "scripts/ci_dependency_audit.py",
        "scripts/ci_mutation/__main__.py",
        "scripts/ci_mutation/browser.py",
        "scripts/ci_wiring.py",
        "scripts/packaging/compose-hive-smoke.sh",
        "scripts/packaging/hive-join-smoke.sh",
        "scripts/packaging/swarm-smoke.sh",
        "scripts/size_limits.py",
        "scripts/swarm_ledger/artifact_sanity.py",
        "tests/count_floor.py",
        "tests/coverage_baseline.py",
        "tests/coverage_ratchet.py",
        "tests/dev_durations.py",
        "tests/refresh_durations.py",
        "tests/shard_budget.py",
        "tests/shard_check.py",
    ]
    assert {path: count for path, count in calls.items() if count} == {}


def test_each_local_only_exemption_names_a_function_that_calls_the_api():
    for path, names in LOCAL_ONLY.items():
        source = path.read_text()
        functions = {node.name: node for node in ast.parse(source).body if isinstance(node, ast.FunctionDef)}
        assert all(API_CALL.search(ast.get_source_segment(source, functions[name])) for name in names)


@pytest.mark.parametrize(
    ("call", "script"),
    [
        ("bash .github/coverage/combine.sh --downloaded 8 || status=$?", ".github/coverage/combine.sh"),
        ("timeout 60s python -m tests.shard_check --shards 8", "tests/shard_check.py"),
        ("python -m scripts.ci_mutation report", "scripts/ci_mutation/__main__.py"),
        ("./scripts/brain-smoke --no-color", "scripts/brain-smoke"),
        (".github/coverage/combine.sh --downloaded 8", ".github/coverage/combine.sh"),
        ("bash -x .github/coverage/combine.sh", ".github/coverage/combine.sh"),
        ('"./scripts/brain-smoke"', "scripts/brain-smoke"),
        ("python -I -m tests.shard_check", "tests/shard_check.py"),
        ("python -W ignore -m tests.shard_check", "tests/shard_check.py"),
        ('bash "${GITHUB_WORKSPACE}/.github/coverage/combine.sh"', ".github/coverage/combine.sh"),
    ],
    ids=[
        "bash-path",
        "python-module",
        "python-package",
        "executable",
        "bare-path",
        "interpreter-flag",
        "quoted-executable",
        "python-flag",
        "python-option-value",
        "workspace-path",
    ],
)
def test_each_way_a_step_runs_a_script_is_read(call, script):
    assert list(_called(call, ROOT)) == [ROOT / script]


def test_a_script_outside_the_caller_is_not_read(tmp_path):
    (tmp_path / "outer.sh").write_text("gh api rate_limit\n")
    (tmp_path / "action").mkdir()
    assert list(_called('bash "$GITHUB_ACTION_PATH/../outer.sh"', tmp_path / "action")) == []


def test_an_api_call_outside_the_exempt_functions_is_counted(tmp_path, monkeypatch):
    script = tmp_path / "durations.py"
    script.write_text('def fetch():\n    return gh(["api"])\n\n\ndef merge():\n    return 1\n')
    monkeypatch.setitem(LOCAL_ONLY, script.resolve(), {"fetch"})
    run = [('bash "$GITHUB_ACTION_PATH/durations.py"', tmp_path)]
    assert _api_calls(run) == {script.resolve(): 0}
    script.write_text(script.read_text().replace("return 1", 'return urlopen("https://api.github.com/x")'))
    assert _api_calls(run) == {script.resolve(): 1}


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
        {"env": {"GH_TOKEN": "${{ secrets['GITHUB_TOKEN'] }}"}},
        {"env": {"GH_TOKEN": '${{ secrets["GH_PAT"] }}'}},
        {"env": {"GH_TOKEN": "${{ secrets [ 'github_token' ] }}"}},
        {"env": {"GH_TOKEN": "${{ secrets['GITHUB_TOKEN'] }} \"quoted\""}},
        {"with": {"token": "${{ secrets\n  .GITHUB_TOKEN }}"}},
        {"run": "echo ${{ github['token'] }}"},
        {"with": {"github-token": '${{ github["token"] }}'}},
        {"env": {"ALL": "${{ toJSON(secrets) }}"}},
        {"run": "echo '${{ tojson( github ) }}'"},
        {"env": {"ALL": "${{ secrets }}"}},
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
        "secret-in-brackets",
        "secret-in-double-quoted-brackets",
        "secret-in-spaced-brackets",
        "secret-in-brackets-beside-double-quotes",
        "secret-across-lines",
        "github-token-in-brackets",
        "github-token-in-double-quoted-brackets",
        "secrets-context-to-json",
        "github-context-to-json",
        "whole-secrets-context",
    ],
)
def test_each_way_of_reaching_the_api_is_an_offender(plant):
    assert _holds_token(plant) or API_CALL.search(plant.get("run", ""))


@pytest.mark.parametrize(
    "value",
    [
        "${{ github.event_name }}",
        "${{ github['event_name'] }}",
        "${{ toJSON(github.event) }}",
        "${{ secrets.SONAR_TOKEN }}",
        "${{ secrets['SONAR_TOKEN'] }}",
        "${{ contains(github.ref, 'github') }}",
        "https://github.com/the-cloud-clockwork/agentihooks",
        'case "$host" in github) exit 0;; esac',
        "see (the docs on github)",
    ],
    ids=[
        "event-name",
        "bracketed-event-name",
        "event-to-json",
        "other-secret",
        "bracketed-other-secret",
        "quoted-word",
        "url",
        "shell-case-label",
        "prose-in-parentheses",
    ],
)
def test_a_value_without_the_workflow_token_holds_none(value):
    assert not _holds_token({"env": {"VALUE": value}, "run": f"echo '{value}'"})


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
