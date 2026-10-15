import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.ci_semgrep import main, scan, timed_out_only

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).parent.parent
RULE = "yaml.github-actions.security.run-shell-injection.run-shell-injection"
TIMEOUT = {"type": "Timeout", "rule_id": RULE, "path": ".github/workflows/test.yml"}
FINDING = {"check_id": RULE, "path": ".github/workflows/test.yml"}
STUB = """
import json, sys
from pathlib import Path
plan_path = Path(sys.argv[1])
plan = json.loads(plan_path.read_text())
code, report = plan.pop(0)
plan_path.write_text(json.dumps(plan))
output = next(a.split("=", 1)[1] for a in sys.argv if a.startswith("--json-output="))
if report is not None:
    Path(output).write_text(json.dumps(report))
with open(plan_path.with_suffix(".calls"), "a") as calls:
    calls.write(json.dumps(sys.argv[2:]) + "\\n")
print("semgrep ran", flush=True)
sys.exit(code)
"""


def _semgrep(tmp_path: Path, *attempts: tuple[int, dict | None]) -> tuple[list[str], Path]:
    stub = tmp_path / "semgrep_stub.py"
    stub.write_text(STUB)
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps(list(attempts)))
    return [sys.executable, str(stub), str(plan), "scan", "--strict"], plan.with_suffix(".calls")


def _calls(calls: Path) -> list[list[str]]:
    return [json.loads(line) for line in calls.read_text().splitlines()]


def test_a_scan_whose_only_errors_are_timeouts_runs_once_more_and_grades_the_second_run(tmp_path, capfd):
    other = {**TIMEOUT, "rule_id": "a.rule", "path": "b.yml"}
    command, calls = _semgrep(
        tmp_path, (2, {"results": [], "errors": [TIMEOUT, other]}), (0, {"results": [], "errors": []})
    )
    report = tmp_path / "semgrep.json"
    assert scan(command, report) == 0
    assert _calls(calls) == [["scan", "--strict", f"--json-output={report}"]] * 2
    captured = capfd.readouterr()
    assert captured.out.splitlines() == ["semgrep ran", "semgrep ran"]
    assert captured.err.splitlines() == [
        "Semgrep timed out with no other error, scanning once more: "
        f"a.rule on b.yml, {RULE} on .github/workflows/test.yml"
    ]


def test_a_rule_that_times_out_twice_stays_red(tmp_path):
    timed_out = (2, {"results": [], "errors": [TIMEOUT]})
    command, calls = _semgrep(tmp_path, timed_out, timed_out)
    assert scan(command, tmp_path / "semgrep.json") == 2
    assert len(_calls(calls)) == 2


@pytest.mark.parametrize(
    "first",
    [
        (1, {"results": [FINDING], "errors": [TIMEOUT]}),
        (2, {"results": [], "errors": [TIMEOUT, {**TIMEOUT, "type": "Syntax error"}]}),
        (2, {"results": [], "errors": []}),
        (2, None),
        (0, {"results": [], "errors": [TIMEOUT]}),
    ],
    ids=["finding", "other-error", "no-error", "no-report", "passed"],
)
def test_anything_but_a_failed_timeout_only_scan_is_graded_as_it_ran(tmp_path, first):
    command, calls = _semgrep(tmp_path, first, (0, {"results": [], "errors": []}))
    assert scan(command, tmp_path / "semgrep.json") == first[0]
    assert len(_calls(calls)) == 1


def test_timed_out_only_names_each_timed_out_rule_and_file_once(tmp_path):
    report = tmp_path / "semgrep.json"
    names = ["e", "d", "c", "b", "a", "e"]
    errors = [{**TIMEOUT, "rule_id": f"{name}.rule", "path": f"{name}.yml"} for name in names]
    report.write_text(json.dumps({"results": [], "errors": errors}))
    assert timed_out_only(report) == [f"{name}.rule on {name}.yml" for name in "abcde"]
    report.write_text(json.dumps({"results": []}))
    assert timed_out_only(report) == []
    report.write_text("{")
    assert timed_out_only(report) == []


def test_main_scans_into_a_report_of_its_own_and_grades_the_rescan(tmp_path):
    command, calls = _semgrep(tmp_path, (2, {"results": [], "errors": [TIMEOUT]}), (1, {"results": [FINDING]}))
    assert main(command) == 1
    first, second = _calls(calls)
    assert first == second
    assert first[:2] == ["scan", "--strict"]
    assert Path(first[2].removeprefix("--json-output=")).name == "semgrep.json"
    assert not Path(first[2].removeprefix("--json-output=")).parent.exists()


def test_the_program_passes_the_scan_exit_code_through(tmp_path):
    command, calls = _semgrep(tmp_path, (2, {"results": [], "errors": [TIMEOUT]}), (1, {"results": [FINDING]}))
    done = subprocess.run([sys.executable, str(_ROOT / "scripts/ci_semgrep.py"), *command], capture_output=True)
    assert done.returncode == 1
    assert len(_calls(calls)) == 2
    assert done.stderr.decode().startswith("Semgrep timed out with no other error")
