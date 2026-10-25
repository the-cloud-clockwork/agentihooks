import ast
import json
import os
import re
from pathlib import Path

import pytest
import yaml

from scripts import ci_dev_reuse, ci_test_plan
from tests import coverage_stability, refresh_durations

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]
API_CALL = re.compile(r"\b_?gh\b(?!-)|api\.github\.com|GITHUB_API_URL")
MODULE = re.compile(r"\bpython3?\b[^\n;&|]*?\s-m\s+([\w.]+)")
BESIDE = re.compile(r"\$\(dirname \"\$0\"\)|\$\{?GITHUB_ACTION_PATH\}?")
WORKSPACE = re.compile(r"\$\{?GITHUB_WORKSPACE\}?")
# These API functions run only in local downloads or steps bound to the App token.
LOCAL_ONLY = {
    ROOT / "scripts/ci_budget/rate_limit.py": {"reset_wait", "_run"},
    ROOT / "scripts/ci_mutation/reuse.py": {"api"},
    ROOT / "tests/refresh_durations.py": {"_gh", "ci_run_ids", "ci_download"},
}
METADATA_ONLY = {
    ROOT / "scripts/ci_reuse.py": {"paged_reuse_job"},
    ROOT / "scripts/ci_gate_queue.py": {"watcher"},
}
TOKEN = re.compile(
    r"github\s*(\.\s*token|\[\s*['\"]token['\"]\s*\])"
    r"|secrets\s*(\.|\[\s*['\"])\s*(github|gh)(_\w*|\w*(token|pat)(?![a-z]))"
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
        yield name, job
        if called.startswith("./.github/workflows/"):
            for inner, inner_job in _workflow(Path(called).name)["jobs"].items():
                yield f"{name}/{inner}", inner_job


def _all_steps():
    for name, job in _all_jobs():
        for key in ("env", "with", "secrets"):
            values = {key: "${{ secrets }}"} if job.get(key) == "inherit" else job.get(key)
            if isinstance(values, dict) and TOKEN.search(_values(values)):
                yield name, {"name": f"job {key}", "env": values}, ROOT
        for step in job.get("steps", []):
            action = step.get("uses", "").replace("./.workflow-revision/", "./", 1)
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
        or (
            step.get("uses", "").startswith("actions/github-script@")
            and (step.get("with") or {}).get("github-token") != APP_TOKEN
        )
    )


def _offends(step: dict) -> bool:
    on_app_quota = (step.get("env") or {}).get("GH_TOKEN") == APP_TOKEN
    return _holds_token(step) or (bool(API_CALL.search(step.get("run", ""))) and not on_app_quota)


def test_no_step_on_any_event_holds_the_workflow_token_or_calls_the_api():
    offenders = []
    for job, step, _ in _all_steps():
        if job == "select" and step.get("id") == "select":
            env = step.get("env") or {}
            assert env["GH_TOKEN"] == "${{ steps.app-token.outputs.token || github.token }}"
            step = {**step, "env": {key: value for key, value in env.items() if key != "GH_TOKEN"}}
        if _offends(step):
            offenders.append(f"{job}: {step.get('name') or step.get('uses')}")
    assert offenders == []


@pytest.mark.parametrize("called", ["./.github/workflows/called.yml", "o/r/.github/workflows/called.yml@v1"])
@pytest.mark.parametrize(
    ("passed", "offenders"),
    [
        ({"with": {"token": "${{ github.token }}"}}, ["caller"]),
        ({"secrets": {"token": "${{ secrets.GITHUB_TOKEN }}"}}, ["caller"]),
        ({"secrets": "inherit"}, ["caller"]),
    ],
    ids=["with-input", "secrets-mapping", "secrets-inherit"],
)
def test_a_reusable_workflow_call_passing_the_workflow_token_is_an_offender(monkeypatch, called, passed, offenders):
    caller = {"uses": called, **passed}
    monkeypatch.setitem(globals(), "_jobs", lambda: {"caller": caller})
    monkeypatch.setitem(globals(), "_workflow", lambda name: {"jobs": {"inner": {"steps": []}}})
    assert [job for job, step, _ in _all_steps() if _offends(step)] == offenders


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
    exemptions = LOCAL_ONLY.get(path, set()) | METADATA_ONLY.get(path, set())
    for node in ast.parse("\n".join(lines)).body if exemptions else []:
        if isinstance(node, ast.FunctionDef) and node.name.removeprefix("x_").partition("__mutmut_")[0] in exemptions:
            lines[node.lineno - 1 : node.end_lineno] = [""] * (node.end_lineno - node.lineno + 1)
    return "\n".join(lines)


def test_no_script_a_step_runs_calls_the_api():
    calls = _api_calls((step.get("run", ""), here) for _, step, here in _all_steps())
    expected = [
        ".github/actions/browser-cache/select-artifacts.sh",
        ".github/actions/browser-cache/verify.sh",
        ".github/coverage/combine.sh",
        ".github/coverage/flatten.py",
        ".github/coverage/proxy.py",
        ".github/coverage/sonar-retry.sh",
        ".github/scripts/pypi-retry.sh",
        "deploy/helm/agentihooks-swarm/ci/kind-smoke.sh",
        "docker/swarm-node/base-copy.sh",
        "docker/swarm-node/pull-base.sh",
        "docker/swarm-node/smoke.sh",
        "hooks/__main__.py",
        "hooks/hook_manager.py",
        "hooks/targets/normalizer.py",
        "scripts/brain-smoke",
        "scripts/ci_budget/__main__.py",
        "scripts/ci_budget/jobs.py",
        "scripts/ci_budget/rate_limit.py",
        "scripts/ci_cpu_quota.py",
        "scripts/ci_dependency_audit.py",
        "scripts/ci_dev_reuse.py",
        "scripts/ci_gate_summary.py",
        "scripts/ci_hive_nats_grants.py",
        "scripts/ci_input_key.py",
        "scripts/ci_mutation/__main__.py",
        "scripts/ci_mutation/plan.py",
        "scripts/ci_mutation/reuse.py",
        "scripts/ci_release.sh",
        "scripts/ci_semgrep_floors.py",
        "scripts/ci_unit_rerun.py",
        "scripts/ci_wiring.py",
        "scripts/hive/nats_grant.py",
        "scripts/packaging/compose-hive-smoke.sh",
        "scripts/packaging/compose-recovery-drill.sh",
        "scripts/packaging/compose-task-smoke.sh",
        "scripts/packaging/hive-join-smoke.sh",
        "scripts/packaging/hive-launch-attestation-proof.sh",
        "scripts/packaging/hive-launch-smoke.sh",
        "scripts/packaging/pull-runtime-image.sh",
        "scripts/packaging/swarm-image-clients.sh",
        "scripts/packaging/swarm-init-smoke.sh",
        "scripts/packaging/swarm-smoke.sh",
        "scripts/packaging/swarm-smoke-parallel.sh",
        "scripts/size_limits.py",
        "scripts/swallowed_exceptions.py",
        "scripts/swarm_ledger/artifact_sanity.py",
        "scripts/swarm_ledger/ledger_server.py",
        "tests/chart_examples.py",
        "tests/chart_github_auth.py",
        "tests/chart_hive_home.py",
        "tests/chart_hive_join.py",
        "tests/chart_hive_only.py",
        "tests/chart_hub.py",
        "tests/chart_nats.py",
        "tests/chart_network.py",
        "tests/chart_redis.py",
        "tests/chart_scratch_swarms.py",
        "tests/chart_secrets.py",
        "tests/chart_workers.py",
        "tests/ci_charts/chart_timers.py",
        "tests/count_floor.py",
        "tests/coverage_check.py",
        "tests/coverage_baseline.py",
        "tests/coverage_ratchet.py",
        "tests/dep_manifest.py",
        "tests/dev_durations.py",
        "tests/flake_budget.py",
        "tests/integration/swarm_node/run_health_proof.sh",
        "tests/js_lcov.py",
        "tests/kind_request.py",
        "tests/ledger_load/gate.py",
        "tests/ledger_load/hierarchy_paired.py",
        "tests/ledger_load/hierarchy_probe.py",
        "tests/ledger_load/memory.py",
        "tests/ledger_load/paired.py",
        "tests/ledger_load/streams.py",
        "tests/refresh_durations.py",
        "tests/reused_coverage.py",
        "tests/shard_budget.py",
        "tests/shard_check.py",
        "tests/shard_summary.py",
        "scripts/ci_select.py",
        "tests/__init__.py",
        "tests/browser_coverage.py",
        "tests/flake_ledger.py",
        "tests/routing/test_codex_api.py",
        "tests/shards.py",
        "tests/swarm_ledger/test_postgres_writer_lock.py",
        "tests/swarm_ledger/test_postgres_outbox.py",
        "tests/swarm_ledger/test_postgres_writer_floor.py",
        "tests/swarm_ledger/test_postgres_repository.py",
        "tests/test_suite_speed.py",
        "scripts/ci_reuse.py",
        "scripts/ci_runner_classes.py",
    ]
    if (ROOT / "scripts/ci_runner_trust.py").exists():
        expected.extend(("scripts/ci_runner_trust.py", "scripts/gate_protected.py"))
    if (ROOT / "scripts/ci_wall_clock.py").exists():
        expected.append("scripts/ci_wall_clock.py")
    if (ROOT / ".github/actions/browser-cache/use-image.sh").exists():
        expected.append(".github/actions/browser-cache/use-image.sh")
    routing_contracts = [
        "tests/test_ci_anton_runners.py",
        "tests/test_ci_runner_classes.py",
        "tests/test_ci_runner_trust.py",
    ]
    expected += [name for name in ("scripts/ci_gate_shards.py", "scripts/ci_gate_queue.py") if ROOT / name in calls]
    assert sorted(str(path.relative_to(ROOT)) for path in calls) in (
        sorted(expected),
        sorted(expected + routing_contracts),
        sorted(expected + ["tests/queue_shards.py"]),
        sorted(expected + routing_contracts + ["tests/queue_shards.py"]),
    )
    assert {path: count for path, count in calls.items() if count} == {}


def test_each_local_only_exemption_names_a_function_that_calls_the_api():
    for path, names in LOCAL_ONLY.items():
        source = path.read_text()
        functions = {node.name: node for node in ast.parse(source).body if isinstance(node, ast.FunctionDef)}
        assert all(API_CALL.search(ast.get_source_segment(source, functions[name])) for name in names)


def test_the_reuse_factory_exemption_contains_only_command_replacements():
    for path, names in METADATA_ONLY.items():
        source = path.read_text()
        functions = {node.name: node for node in ast.parse(source).body if isinstance(node, ast.FunctionDef)}
        for name in names:
            if path == ROOT / "scripts/ci_gate_queue.py":
                assert name == "watcher"
                assert len(functions[name].body) == 1
                assert isinstance(functions[name].body[0], ast.Return)
                assert isinstance(functions[name].body[0].value, ast.Dict)
                assert not any(isinstance(node, ast.Call) for node in ast.walk(functions[name]))
                continue
            calls = [
                node
                for node in ast.walk(functions[name])
                if isinstance(node, ast.Call) and API_CALL.search(ast.get_source_segment(source, node))
            ]
            assert calls
            assert all(isinstance(node.func, ast.Attribute) and node.func.attr == "replace" for node in calls)


def test_the_budget_reader_runs_only_on_the_app_token():
    reader = ROOT / "scripts/ci_budget/jobs.py"
    steps = [step for _, step, here in _all_steps() if reader in _called(step.get("run", ""), here)]
    assert steps
    assert all(step.get("env", {}).get("GH_TOKEN") == APP_TOKEN and not _holds_token(step) for step in steps)


def test_the_preflight_reuse_lookup_runs_only_on_the_app_token():
    lookup = ROOT / "scripts/ci_mutation/reuse.py"
    steps = [step for _, step, here in _all_steps() if lookup in _called(step.get("run", ""), here)]
    assert steps
    assert all(step.get("env", {}).get("GH_TOKEN") == APP_TOKEN and not _holds_token(step) for step in steps)


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


@pytest.mark.parametrize("exemptions", [LOCAL_ONLY, METADATA_ONLY])
def test_an_api_call_outside_the_exempt_functions_is_counted(tmp_path, monkeypatch, exemptions):
    script = tmp_path / "durations.py"
    script.write_text('def fetch():\n    return gh(["api"])\n\n\ndef merge():\n    return 1\n')
    monkeypatch.setitem(exemptions, script.resolve(), {"fetch"})
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
    ("binding", "offends"),
    [(APP_TOKEN, False), ("${{ github.token }}", True), ("", True), ("${{ secrets.OTHER_TOKEN }}", True)],
)
def test_github_script_requires_the_explicit_app_token(binding, offends):
    step = {"uses": "actions/github-script@v7", "with": {"github-token": binding}}
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
        {"env": {"GH_TOKEN": "${{ secrets.GHCR_TOKEN }}"}},
        {"with": {"token": "${{ secrets['GITHUBTOKEN'] }}"}},
        {"env": {"KEY": "${{ secrets.GH_API_KEY }}"}},
        {"env": {"GH_TOKEN": "${{ secrets.GHCR_TOKEN_V2 }}"}},
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
        "ghcr-secret",
        "github-secret-without-separator",
        "gh-secret-not-named-token",
        "ghcr-secret-with-suffix",
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
        "${{ secrets.GHOST_KEY }}",
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
        "gh-prefixed-other-secret",
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
    assert sonar["needs"] == ["unit", "unit-selected", "select", "reuse", "admission"]
    steps = sonar["steps"]
    download = next(step for step in steps if step.get("name") == "Download shard coverage")
    merge = next(step for step in steps if step.get("name") == "Merge shard coverage")
    assert download["if"] == merge["if"] == "needs.select.outputs.scope != 'none'"
    assert download["uses"].startswith("actions/download-artifact@")
    assert download["with"] == {"pattern": "coverage-3.12-*", "path": ".coverage-shards"}
    assert steps.index(download) < steps.index(merge)
    assert merge["env"] == {
        "SHARDS": "${{ needs.select.outputs.scope == 'selected' && needs.select.outputs.count || 20 }}",
        "SCOPE": "${{ needs.select.outputs.scope }}",
    }
    assert 'bash .github/coverage/combine.sh --downloaded "$SHARDS" ||' in merge["run"]


def test_every_reader_of_the_unit_shard_count_matches_the_matrix():
    jobs = _jobs()
    shards = jobs["unit"]["strategy"]["matrix"]["shard"]
    count = len(shards)
    assert shards == list(range(1, count + 1))
    run = next(step for step in jobs["unit"]["steps"] if step.get("name") == "Run tests")
    assert (
        run["env"]["SHARD"]
        == f"${{{{ matrix.shard }}}}/${{{{ matrix.python-version == '3.12' && {count} || {count} }}}}"
    )
    floor = jobs["unit"]["strategy"]["matrix"]["exclude"]
    assert [entry["shard"] for entry in floor] == shards[12:]
    assert ci_test_plan.UNIT_JOBS == {f"unit (3.12, {shard})" for shard in shards}
    assert ci_test_plan.COVERAGE == {f"coverage-3.12-{shard}" for shard in shards}
    assert {f"coverage-3.12-{shard}" for shard in shards} <= ci_dev_reuse.artifact_names()
    assert coverage_stability.SHARDS == refresh_durations.SHARDS == count
    workflow = (ROOT / ".github/workflows/test.yml").read_text()
    assert workflow.count(f"--shards {count} \\") == 2
    assert f"needs.select.outputs.count || {count} }}}}" in workflow


@pytest.mark.parametrize("job", ["unit", "shard-check", "test-count", "lint"])
def test_a_dev_push_runs_every_step_of_the_job_itself(job):
    steps = _jobs()[job]["steps"]
    assert steps[0]["uses"] == "actions/checkout@v4"
    assert "if" not in steps[0]
    assert [step.get("name") for step in steps if "skip" in step.get("if", "")] == []


@pytest.mark.parametrize("job", sorted(_jobs()))
def test_no_job_is_granted_the_actions_api(job):
    permissions = _jobs()[job].get("permissions") or {}
    if job == "select":
        assert permissions in ({"contents": "read"}, {"contents": "read", "actions": "read"})
    else:
        assert "actions" not in permissions


def test_no_job_skips_on_a_tree_another_run_passed():
    jobs = _jobs()
    assert "record-pass" not in jobs
    assert "outputs" not in jobs["lint"]
    assert "steps.lookup" not in (ROOT / ".github/workflows/test.yml").read_text()


def test_sonar_scans_the_graded_revision_without_reading_the_dev_head():
    steps = _jobs()["sonar"]["steps"]
    dev_head = re.compile(r"refs/heads/dev|origin[ /]\+?dev\b|:dev\b|github\.event\.after")
    assert [step.get("name", step.get("uses")) for step in steps if dev_head.search(json.dumps(step))] == []
    assert all("steps.current" not in step.get("if", "") for step in steps)
    scan = next(step for step in steps if step.get("name") == "SonarQube Scan")
    gate = next(step for step in steps if step.get("name") == "SonarQube Quality Gate")
    assert "if" not in scan
    assert "if" not in gate
