import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
import yaml

from tests import leaks

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).parent.parent
POLLUTER = "tests/test_a_polluter.py::test_sets_a_setting"
VICTIM = "tests/test_b_victim.py::test_needs_a_clean_environment"
PAIR = {"victim": "tests/test_b.py::test_v", "polluter": "tests/test_a.py::test_p"}


def _workflow() -> dict:
    return yaml.safe_load((_ROOT / ".github/workflows/test-leaks.yml").read_text())


def _job() -> dict:
    return _workflow()["jobs"]["one-process"]


def _step(step_id: str) -> dict:
    (step,) = [step for step in _job()["steps"] if step.get("id") == step_id]
    return step


def test_leak_run_is_scheduled_and_checks_out_dev():
    triggers = _workflow()[True]
    assert triggers["schedule"]
    assert "workflow_dispatch" in triggers
    checkout = next(step for step in _job()["steps"] if step.get("uses", "").startswith("actions/checkout"))
    assert checkout["with"]["ref"] == "${{ github.event_name == 'schedule' && 'dev' || '' }}"


def test_leak_run_never_runs_on_pull_requests():
    triggers = _workflow()[True]
    assert set(triggers) == {"schedule", "workflow_dispatch"}


def test_leak_run_covers_file_order_and_random_order_side_by_side():
    strategy = _job()["strategy"]
    assert strategy["matrix"]["order"] == ["fixed", "random"]
    assert strategy["fail-fast"] is False


@pytest.mark.parametrize(("seed", "order_flags"), [("", []), ("42", ["--random-order-seed=42"])])
def test_leak_run_runs_the_whole_suite_in_one_process(tmp_path, seed, order_flags):
    step = _step("suite")
    assert step["env"]["SEED"] == "${{ matrix.order == 'random' && github.run_id || '' }}"
    tools = tmp_path / "bin"
    tools.mkdir()
    (tools / "python").write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$@" > "$ARGS"\n')
    (tools / "python").chmod(0o755)
    env = {**os.environ, "PATH": f"{tools}:{os.environ['PATH']}", "ARGS": str(tmp_path / "args"), "SEED": seed}
    subprocess.run(["bash", "-e", "-c", step["run"]], env=env, check=True)
    argv = (tmp_path / "args").read_text().splitlines()
    assert argv[:3] == ["-m", "pytest", "tests/"]
    assert argv[argv.index("-n") + 1] == "0"
    assert "--report-log=run.jsonl" in argv
    assert [arg for arg in argv if arg.startswith("--random-order")] == order_flags


def test_a_red_run_names_its_pairs_and_keeps_them_for_the_follow_up_job():
    pairs = _step("pairs")
    assert pairs["if"] == "failure() && steps.suite.outcome == 'failure'"
    assert pairs["env"] == {"ORDER": "${{ matrix.order }}"}
    assert pairs["run"] == 'python -m tests.leaks pairs run.jsonl "leaks-$ORDER.json" | tee -a "$GITHUB_STEP_SUMMARY"'
    keep = _step("keep")
    assert keep["if"] == pairs["if"]
    assert keep["uses"].startswith("actions/upload-artifact@")
    assert keep["with"]["name"] == "leaks-${{ matrix.order }}"
    assert keep["with"]["path"] == "leaks-${{ matrix.order }}.json"


def test_follow_ups_open_once_per_run_and_only_for_dev():
    jobs = _workflow()["jobs"]
    assert jobs["one-process"]["permissions"] == {"contents": "read"}
    followup = jobs["followup"]
    assert followup["needs"] == "one-process"
    assert followup["if"] == "${{ failure() && (github.event_name == 'schedule' || github.ref == 'refs/heads/dev') }}"
    assert followup["permissions"] == {"contents": "read", "issues": "write"}
    steps = followup["steps"]
    checkout = next(step for step in steps if step.get("uses", "").startswith("actions/checkout"))
    assert checkout["with"]["ref"] == "${{ github.event_name == 'schedule' && 'dev' || '' }}"
    download = next(step for step in steps if step.get("uses", "").startswith("actions/download-artifact"))
    assert download["with"] == {"pattern": "leaks-*", "merge-multiple": True}
    opener = next(step for step in steps if step.get("name") == "Open a follow up per finding")
    assert opener["env"]["GH_TOKEN"] == "${{ github.token }}"
    assert opener["run"] == (
        'python -m tests.leaks followup "$GITHUB_SERVER_URL/$GITHUB_REPOSITORY/actions/runs/$GITHUB_RUN_ID" leaks-*.json'
    )


@pytest.mark.parametrize("tool", ["pytest-random-order", "pytest-reportlog", "detect-test-pollution"])
def test_dev_extra_declares_the_leak_tools(tool):
    dev = tomllib.loads((_ROOT / "pyproject.toml").read_text())["project"]["optional-dependencies"]["dev"]
    assert any(dep.startswith(tool) for dep in dev)


def _suite(tmp_path: Path, leak: bool) -> Path:
    project = tmp_path / "project"
    (project / "tests").mkdir(parents=True)
    setter = "os.environ['LEAKS_PLANTED'] = '1'" if leak else "monkeypatch.setenv('LEAKS_PLANTED', '1')"
    (project / "tests/test_a_polluter.py").write_text(
        f"import os\n\n\ndef test_sets_a_setting(monkeypatch):\n    {setter}\n"
    )
    (project / "tests/test_b_victim.py").write_text(
        "import os\n\n\ndef test_needs_a_clean_environment():\n    assert 'LEAKS_PLANTED' not in os.environ\n"
    )
    (project / "tests/test_c_bystander.py").write_text("def test_passes():\n    pass\n")
    return project


def _run_suite(project: Path, monkeypatch) -> int:
    monkeypatch.delenv("PYTEST_ADDOPTS", raising=False)
    monkeypatch.delenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", raising=False)
    monkeypatch.chdir(project)
    command = [sys.executable, "-m", "pytest", "tests", "-n", "0", "-p", "no:cacheprovider", "--report-log=run.jsonl"]
    return subprocess.run(command, capture_output=True, check=False).returncode


def test_a_planted_leak_is_named_as_the_pair_that_fails(tmp_path, monkeypatch, capsys):
    project = _suite(tmp_path, leak=True)
    assert _run_suite(project, monkeypatch) == 1
    leaks.main(["pairs", "run.jsonl", "leaks.json"])
    assert json.loads((project / "leaks.json").read_text()) == [{"victim": VICTIM, "polluter": POLLUTER}]
    assert f"| `{POLLUTER}` | `{VICTIM}` |" in capsys.readouterr().out


def test_the_same_suite_without_the_leak_passes_and_names_nothing(tmp_path, monkeypatch):
    project = _suite(tmp_path, leak=False)
    assert _run_suite(project, monkeypatch) == 0
    assert leaks.leaking_pairs(project / "run.jsonl", project) == ([], [])


def test_a_test_that_fails_alone_is_not_called_a_leak(tmp_path, monkeypatch):
    project = _suite(tmp_path, leak=False)
    (project / "tests/test_b_victim.py").write_text("def test_needs_a_clean_environment():\n    assert False\n")
    assert _run_suite(project, monkeypatch) == 1
    assert leaks.leaking_pairs(project / "run.jsonl", project) == (
        [{"victim": VICTIM, "polluter": None, "note": "it fails when run alone"}],
        [],
    )


def test_a_failure_in_setup_counts_as_a_failing_test(tmp_path):
    log = tmp_path / "run.jsonl"
    reports = [
        ("tests/t.py::a", "setup", "passed"),
        ("tests/t.py::a", "call", "passed"),
        ("tests/t.py::b", "setup", "failed"),
    ]
    log.write_text(
        "".join(
            json.dumps({"$report_type": "TestReport", "nodeid": nodeid, "when": when, "outcome": outcome}) + "\n"
            for nodeid, when, outcome in reports
        )
    )
    assert leaks.run_order(log) == (["tests/t.py::a", "tests/t.py::b"], ["tests/t.py::b"])


def test_only_the_first_failing_tests_are_traced_and_the_rest_are_named(tmp_path, monkeypatch):
    log = tmp_path / "run.jsonl"
    nodeids = [f"tests/t.py::test_{i}" for i in range(leaks.MAX_TRACED + 2)]
    log.write_text(
        "".join(
            json.dumps({"$report_type": "TestReport", "nodeid": nodeid, "when": "call", "outcome": "failed"}) + "\n"
            for nodeid in nodeids
        )
    )
    monkeypatch.setattr(leaks, "find_polluter", lambda victim, before, cwd: {"victim": victim, "before": before})
    findings, untraced = leaks.leaking_pairs(log, tmp_path)
    assert findings == [
        {"victim": nodeid, "before": nodeids[:i]} for i, nodeid in enumerate(nodeids[: leaks.MAX_TRACED])
    ]
    assert untraced == nodeids[leaks.MAX_TRACED :]
    assert all(f"`{nodeid}`" in leaks.summary([], untraced) for nodeid in untraced)


def _fake_gh(open_issues: list[dict]):
    calls = []

    def gh(args):
        calls.append(args)
        return json.dumps(open_issues) if args[:2] == ["issue", "list"] else ""

    return gh, calls


def _body(call: list[str]) -> str:
    return call[call.index("--body") + 1]


def test_a_new_pair_opens_a_follow_up_naming_it():
    gh, calls = _fake_gh([{"number": 7, "title": "Something else"}])
    leaks.open_followups([PAIR], "https://example.test/runs/1", gh)
    listing, create = calls
    assert listing[:4] == ["issue", "list", "--state", "open"]
    assert create[:4] == [
        "issue",
        "create",
        "--title",
        "Test leak: tests/test_a.py::test_p breaks tests/test_b.py::test_v",
    ]
    assert "python -m pytest -n 0 tests/test_a.py::test_p tests/test_b.py::test_v" in _body(create)
    assert "https://example.test/runs/1" in _body(create)


def test_a_known_pair_gets_a_comment_instead_of_a_duplicate():
    gh, calls = _fake_gh([{"number": 9, "title": "Test leak: tests/test_a.py::test_p breaks tests/test_b.py::test_v"}])
    leaks.open_followups([PAIR], "https://example.test/runs/2", gh)
    (comment,) = calls[1:]
    assert comment[:3] == ["issue", "comment", "9"]
    assert "https://example.test/runs/2" in _body(comment)


def test_a_pair_found_in_both_orders_opens_one_follow_up():
    gh, calls = _fake_gh([])
    leaks.open_followups([PAIR, PAIR], "https://example.test/runs/5", gh)
    assert [call[:2] for call in calls] == [["issue", "list"], ["issue", "create"]]


def test_the_follow_up_reads_the_findings_of_every_order(tmp_path, monkeypatch):
    other = {**PAIR, "victim": "tests/test_c.py::test_w"}
    (tmp_path / "leaks-fixed.json").write_text(json.dumps([PAIR]))
    (tmp_path / "leaks-random.json").write_text(json.dumps([other]))
    seen = []
    monkeypatch.setattr(leaks, "open_followups", lambda findings, run_url: seen.append((findings, run_url)))
    files = [str(tmp_path / "leaks-fixed.json"), str(tmp_path / "leaks-random.json")]
    leaks.main(["followup", "https://example.test/runs/6", *files])
    assert seen == [([PAIR, other], "https://example.test/runs/6")]


def test_a_failure_without_a_pair_opens_a_follow_up_naming_the_test():
    gh, calls = _fake_gh([])
    finding = {"victim": "tests/test_b.py::test_v", "polluter": None, "note": "it fails when run alone"}
    leaks.open_followups([finding], "https://example.test/runs/3", gh)
    create = calls[1]
    assert create[:4] == ["issue", "create", "--title", "Test fails in one process: tests/test_b.py::test_v"]
    assert "it fails when run alone" in _body(create)
    assert "python -m pytest -n 0 tests/test_b.py::test_v" in _body(create)


def test_a_follow_up_title_fits_the_github_limit():
    finding = {"victim": "tests/test_b.py::test_" + "v" * 300, "polluter": "tests/test_a.py::test_p"}
    title, body = leaks.followup(finding, "https://example.test/runs/4")
    assert len(title) == leaks.GITHUB_TITLE_LIMIT
    assert finding["victim"] in body
