import hashlib
import json
import os
import subprocess
import zipfile
from pathlib import Path
from typing import cast

import pytest
import yaml

pytestmark = pytest.mark.unit
_ROOT = Path(__file__).resolve().parents[1]
_VERSION = "7.2.0.5079"


def _sonar():
    return yaml.safe_load((_ROOT / ".github/workflows/test.yml").read_text())["jobs"]["sonar"]


def _step(name):
    return next(step for step in _sonar()["steps"] if step.get("name") == name)


def _tool(bin_dir, name, body):
    path = bin_dir / name
    path.write_text(f"#!/usr/bin/env bash\n{body}\n")
    path.chmod(0o755)


def _run(step, cwd, bin_dir, **env):
    return subprocess.run(
        ["bash", "-e", "-c", step["run"]],
        cwd=cwd,
        env={
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "GITHUB_WORKSPACE": str(_ROOT),
            "GITHUB_EVENT_NAME": "pull_request",
            **env,
        },
        capture_output=True,
        text=True,
        timeout=30,
    )


def _answer(
    tmp_path: Path,
    name: str,
    answer: dict[str, object] | int | list[dict[str, object] | int],
    page: int = 1,
) -> str:
    if isinstance(answer, list):
        later = "".join(_answer(tmp_path, name, value, index) for index, value in enumerate(answer[1:], 2))
        return later + f"  */{name}/search?*\\&p=*) exit 22 ;;\n" + _answer(tmp_path, name, answer[0])
    pattern = f"*/{name}/search?*" + (f"\\&p={page}" if page > 1 else "")
    if isinstance(answer, int):
        return f'  {pattern}) echo "curl: (22) The requested URL returned error: {answer}" >&2; exit 22 ;;\n'
    (tmp_path / f"{name}-{page}.json").write_text(json.dumps(answer))
    return f'  {pattern}) cat "{tmp_path}/{name}-{page}.json" ;;\n'


def _gate(
    tmp_path: Path,
    statuses: list[str],
    gate: str = "OK",
    message: str = "",
    event: str = "push",
    dashboard: str = "http://sonar/dash",
    **searches: dict[str, object] | int | list[dict[str, object] | int],
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (tmp_path / ".scannerwork").mkdir()
    (tmp_path / ".scannerwork/report-task.txt").write_text(f"ceTaskId=T1\ndashboardUrl={dashboard}\n")
    (tmp_path / "statuses").write_text("\n".join(statuses) + "\n")
    task = {"status": "STATUS", "analysisId": "A1", **({"errorMessage": message} if message else {})}
    (tmp_path / "task.json").write_text(json.dumps({"task": task}))
    timeout = searches.pop("timeout", None)
    if isinstance(timeout, int):
        _tool(bin_dir, "timeout", f'printf "%s\\n" timeout "$@" >> "{tmp_path}/timeout"; exit {timeout}')
    found = "".join(_answer(tmp_path, name, answer) for name, answer in searches.items())
    _tool(
        bin_dir,
        "curl",
        f'url="${{@: -1}}"; echo "$url" >> "{tmp_path}/urls"\n'
        f'printf \'%s %s\\n\' "${{@: -2:1}}" "$url" >> "{tmp_path}/headers"\n'
        'case "$url" in\n'
        f'  */ce/task?id=T1) s=$(head -n1 "{tmp_path}/statuses"); sed -i 1d "{tmp_path}/statuses"\n'
        f'    sed "s/STATUS/$s/" "{tmp_path}/task.json" ;;\n'
        f'  */qualitygates/project_status?analysisId=A1) echo \'{{"projectStatus":{{"status":"{gate}"}}}}\' ;;\n'
        f"{found}"
        "  *) exit 22 ;;\n"
        "esac",
    )
    _tool(bin_dir, "sleep", f'echo "$1" >> "{tmp_path}/sleeps"')
    output = tmp_path / "output"
    output.write_text("")
    result = _run(
        _step("SonarQube Quality Gate"),
        tmp_path,
        bin_dir,
        SONAR_TOKEN="t",
        SONAR_READ_TOKEN="r",
        GITHUB_EVENT_NAME=event,
        GITHUB_OUTPUT=str(output),
        GITHUB_STEP_SUMMARY=str(tmp_path / "summary"),
    )
    sleeps = (tmp_path / "sleeps").read_text().split() if (tmp_path / "sleeps").exists() else []
    return result, sleeps


def test_the_quality_gate_is_polled_every_second_until_the_analysis_is_processed(tmp_path):
    result, sleeps = _gate(tmp_path, ["PENDING", "IN_PROGRESS", "SUCCESS"])
    assert result.returncode == 0, result.stdout + result.stderr
    assert sleeps == ["1", "1"]
    assert "Quality Gate has PASSED" in result.stdout


_OLDER = (
    "Date of analysis cannot be older than the date of the last known analysis on this project. "
    'Value: "2026-10-08T01:09:38+0000". Latest analysis: "2026-10-08T01:09:50+0000". '
    "It's only possible to rebuild the past in a chronological order."
)


@pytest.mark.parametrize(
    ("statuses", "gate", "message", "event"),
    [
        (["SUCCESS"], "ERROR", "", "push"),
        (["FAILED"], "OK", "", "push"),
        (["CANCELED"], "OK", "", "push"),
        (["FAILED"], "OK", "Unrecoverable indexing failure", "push"),
        (["CANCELED"], "OK", _OLDER, "push"),
        (["FAILED"], "OK", _OLDER, "pull_request"),
    ],
)
@pytest.mark.feature("sonar-gate-verdict")
def test_a_failed_gate_or_background_task_is_red(tmp_path, statuses, gate, message, event):
    result, _ = _gate(tmp_path, statuses, gate, message, event)
    assert result.returncode != 0
    assert "::error::" in result.stdout
    assert (tmp_path / "output").read_text() == ""


@pytest.mark.feature("sonar-gate-verdict")
def test_a_passed_gate_names_the_analysis_that_grades_the_promoted_revision(tmp_path):
    result, _ = _gate(tmp_path, ["SUCCESS"])
    assert result.returncode == 0, result.stdout + result.stderr
    assert (tmp_path / "output").read_text() == "analysis=A1\n"


_BRANCH = "http://sonar/dashboard?id=agentihooks&branch=dev"
_FAILED = f"::error::Quality Gate has FAILED (ERROR). Detailed information can be found at: {_BRANCH}"
_BUGS: dict[str, object] = {
    "total": 2,
    "issues": [
        {
            "component": "agentihooks:scripts/example.py",
            "line": 7,
            "rule": "python:S905",
            "message": "Remove or refactor this statement; it has no side effects.",
        },
        {
            "component": "agentihooks:scripts/other.py",
            "rule": "python:S5332",
            "message": "Using http protocol is insecure. Use https instead.",
        },
    ],
}


def _searches(tmp_path: Path, endpoint: str = "issues/search") -> list[str]:
    return [url for url in (tmp_path / "urls").read_text().splitlines() if endpoint in url]


@pytest.mark.parametrize("scope", ["branch=dev", "pullRequest=7"])
@pytest.mark.feature("sonar-failed-gate-names-new-bugs")
def test_a_failed_gate_names_its_new_bugs(tmp_path: Path, scope: str) -> None:
    dashboard = f"http://sonar/dashboard?id=agentihooks&{scope}"
    result, _ = _gate(tmp_path, ["SUCCESS"], "ERROR", issues=_BUGS, dashboard=dashboard)
    assert result.returncode == 1
    assert result.stdout.splitlines() == [
        f"::error::Quality Gate has FAILED (ERROR). Detailed information can be found at: {dashboard}",
        "agentihooks:scripts/example.py:7 python:S905 Remove or refactor this statement; it has no side effects.",
        "agentihooks:scripts/other.py:0 python:S5332 Using http protocol is insecure. Use https instead.",
    ]
    assert _searches(tmp_path) == [
        f"http://127.0.0.1:9000/api/issues/search?componentKeys=agentihooks&{scope}"
        "&types=BUG,VULNERABILITY&inNewCodePeriod=true&resolved=false&ps=100"
    ]
    assert (tmp_path / "output").read_text() == ""


@pytest.mark.feature("sonar-failed-gate-names-new-bugs")
def test_a_failed_gate_stays_red_when_the_bug_read_is_refused(tmp_path: Path) -> None:
    result, sleeps = _gate(tmp_path, ["SUCCESS"], "ERROR", issues=400, dashboard=_BRANCH)
    assert result.returncode == 1
    assert result.stdout.splitlines() == [_FAILED]
    assert result.stderr == ""
    assert sleeps == []
    assert len(_searches(tmp_path)) == 1
    assert (tmp_path / "output").read_text() == ""


@pytest.mark.feature("sonar-failed-gate-names-new-bugs")
def test_a_failed_gate_says_how_many_new_bugs_it_left_out(tmp_path: Path) -> None:
    result, _ = _gate(tmp_path, ["SUCCESS"], "ERROR", issues=[{**_BUGS, "total": 102}, 400], dashboard=_BRANCH)
    assert result.returncode == 1
    assert len(result.stdout.splitlines()) == 4
    assert result.stdout.splitlines()[-1] == "100 more not shown"


@pytest.mark.parametrize("status", [503, 403])
@pytest.mark.feature("sonar-failed-gate-names-new-bugs")
def test_a_failed_gate_stays_a_code_verdict_when_the_bug_read_meets_an_outage(tmp_path: Path, status: int) -> None:
    result, sleeps = _gate(tmp_path, ["SUCCESS"], "ERROR", issues=status, dashboard=_BRANCH)
    assert result.returncode == 1
    assert result.stdout.splitlines() == [_FAILED]
    assert result.stderr == ""
    assert not (tmp_path / "summary").exists()
    assert 0 < sum(int(wait) for wait in sleeps) <= 30
    assert (tmp_path / "output").read_text() == ""


@pytest.mark.feature("sonar-failed-gate-names-new-bugs")
def test_a_passed_gate_reads_no_issues(tmp_path: Path) -> None:
    result, _ = _gate(tmp_path, ["SUCCESS"], issues=_BUGS, dashboard=_BRANCH)
    assert result.returncode == 0, result.stdout + result.stderr
    assert _searches(tmp_path) == []


_HOTSPOTS: dict[str, object] = {
    "paging": {"pageIndex": 1, "pageSize": 100, "total": 2},
    "hotspots": [
        {
            "component": "agentihooks:scripts/example.py",
            "line": 12,
            "ruleKey": "python:S5443",
            "message": "Make sure publicly writable directories are used safely here.",
        },
        {
            "component": "agentihooks:hooks/token.py",
            "ruleKey": "python:S2245",
            "message": "Make sure that using this pseudorandom number generator is safe here.",
        },
    ],
}
_HOTSPOT_LINES = [
    "agentihooks:scripts/example.py:12 python:S5443 Make sure publicly writable directories are used safely here.",
    "agentihooks:hooks/token.py:0 python:S2245 Make sure that using this pseudorandom number generator is safe here.",
]


@pytest.mark.parametrize("scope", ["branch=dev", "pullRequest=7"])
@pytest.mark.feature("sonar-failed-gate-names-new-hotspots")
def test_a_failed_gate_names_its_unreviewed_hotspots(tmp_path: Path, scope: str) -> None:
    dashboard = f"http://sonar/dashboard?id=agentihooks&{scope}"
    result, _ = _gate(tmp_path, ["SUCCESS"], "ERROR", dashboard=dashboard, issues=_BUGS, hotspots=_HOTSPOTS)
    assert result.returncode == 1
    assert result.stdout.splitlines() == [
        f"::error::Quality Gate has FAILED (ERROR). Detailed information can be found at: {dashboard}",
        "agentihooks:scripts/example.py:7 python:S905 Remove or refactor this statement; it has no side effects.",
        "agentihooks:scripts/other.py:0 python:S5332 Using http protocol is insecure. Use https instead.",
        *_HOTSPOT_LINES,
    ]
    assert _searches(tmp_path, "hotspots/search") == [
        f"http://127.0.0.1:9000/api/hotspots/search?project=agentihooks&{scope}"
        "&inNewCodePeriod=true&status=TO_REVIEW&ps=100"
    ]
    assert (tmp_path / "output").read_text() == ""


@pytest.mark.feature("sonar-failed-gate-names-new-hotspots")
def test_finding_searches_use_the_read_token_and_polling_keeps_the_scan_token(tmp_path: Path) -> None:
    result, _ = _gate(tmp_path, ["SUCCESS"], "ERROR", dashboard=_BRANCH, issues=_BUGS, hotspots=_HOTSPOTS)
    assert result.returncode == 1
    headers = (tmp_path / "headers").read_text().splitlines()
    assert len(headers) == 4
    assert all(header.startswith("Authorization: Bearer t ") for header in headers[:2])
    assert all(header.startswith("Authorization: Bearer r ") for header in headers[2:])
    assert "/ce/task?" in headers[0]
    assert "/qualitygates/project_status?" in headers[1]
    assert "/issues/search?" in headers[2]
    assert "/hotspots/search?" in headers[3]


@pytest.mark.feature("sonar-failed-gate-names-new-hotspots")
def test_a_failed_gate_says_how_many_hotspots_it_left_out(tmp_path: Path) -> None:
    hotspots = {**_HOTSPOTS, "paging": {"pageIndex": 1, "pageSize": 100, "total": 102}}
    result, _ = _gate(tmp_path, ["SUCCESS"], "ERROR", dashboard=_BRANCH, hotspots=[hotspots, 400])
    assert result.returncode == 1
    assert result.stdout.splitlines() == [_FAILED, *_HOTSPOT_LINES, "100 more hotspots not shown"]


@pytest.mark.feature("sonar-failed-gate-names-new-hotspots")
def test_a_failed_gate_stays_red_when_the_hotspot_read_is_refused(tmp_path: Path) -> None:
    result, sleeps = _gate(tmp_path, ["SUCCESS"], "ERROR", dashboard=_BRANCH, hotspots=400)
    assert result.returncode == 1
    assert result.stdout.splitlines() == [_FAILED]
    assert result.stderr == ""
    assert sleeps == []
    assert len(_searches(tmp_path, "hotspots/search")) == 1
    assert (tmp_path / "output").read_text() == ""


@pytest.mark.parametrize("status", [503, 403])
@pytest.mark.feature("sonar-failed-gate-names-new-hotspots")
def test_a_failed_gate_stays_a_code_verdict_when_the_hotspot_read_meets_an_outage(tmp_path: Path, status: int) -> None:
    result, sleeps = _gate(tmp_path, ["SUCCESS"], "ERROR", dashboard=_BRANCH, hotspots=status)
    assert result.returncode == 1
    assert result.stdout.splitlines() == [_FAILED]
    assert result.stderr == ""
    assert not (tmp_path / "summary").exists()
    assert 0 < sum(int(wait) for wait in sleeps) <= 30
    assert (tmp_path / "output").read_text() == ""


@pytest.mark.parametrize(("statuses", "message"), [(["SUCCESS"], ""), (["FAILED"], _OLDER)])
@pytest.mark.feature("sonar-failed-gate-names-new-hotspots")
def test_a_passed_or_superseded_gate_reads_no_hotspots(tmp_path: Path, statuses: list[str], message: str) -> None:
    result, _ = _gate(tmp_path, statuses, message=message, dashboard=_BRANCH, hotspots=_HOTSPOTS)
    assert result.returncode == 0, result.stdout + result.stderr
    assert _searches(tmp_path, "hotspots/search") == []


def test_a_dev_push_analysis_sonar_refuses_as_older_ends_superseded_and_ungraded(tmp_path):
    result, _ = _gate(tmp_path, ["FAILED"], message=_OLDER)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "::error::" not in result.stdout
    assert "::notice::" in result.stdout
    assert (tmp_path / "output").read_text() == "superseded=true\n"
    assert not any("project_status" in url for url in (tmp_path / "urls").read_text().splitlines())
    assert _searches(tmp_path) == []


def test_the_condition_hold_skips_a_superseded_analysis_and_only_the_gate_names_one():
    hold = _step("Hold the Delivery L2 conditions")
    assert hold["if"] == "always() && steps.proxy.outcome == 'success' && steps.gate.outputs.superseded != 'true'"
    writers = [
        step.get("name")
        for step in _sonar()["steps"]
        if "analysis=" in step.get("run", "") and "GITHUB_OUTPUT" in step.get("run", "")
    ]
    assert writers == ["SonarQube Quality Gate"]


def test_the_gate_step_replaces_the_quality_gate_action():
    sonar = _sonar()
    assert not any("sonarqube-quality-gate-action" in step.get("uses", "") for step in sonar["steps"])
    gate = _step("SonarQube Quality Gate")
    assert "if" not in gate
    assert gate["timeout-minutes"] == 5
    assert not gate.get("continue-on-error")


@pytest.mark.parametrize("name", ["issues", "hotspots"])
@pytest.mark.parametrize("scope", ["branch=dev", "pullRequest=7"])
@pytest.mark.feature("sonar-failed-gate-lists-every-page")
def test_a_failed_gate_lists_every_finding_page(tmp_path: Path, name: str, scope: str) -> None:
    findings = [
        {
            "component": "agentihooks:scripts/page.py",
            "line": line,
            "rule": "python:S905",
            "ruleKey": "python:S905",
            "message": f"Finding {line}",
        }
        for line in range(1, 102)
    ]
    pages = [
        {name: findings[:100], "total": 101, "paging": {"pageIndex": 1, "pageSize": 100, "total": 101}},
        {name: findings[100:], "total": 101, "paging": {"pageIndex": 2, "pageSize": 100, "total": 101}},
    ]
    dashboard = f"http://sonar/dashboard?id=agentihooks&{scope}"
    answers = cast(list[dict[str, object] | int], pages)
    result, _ = _gate(
        tmp_path,
        ["SUCCESS"],
        "ERROR",
        dashboard=dashboard,
        issues=answers if name == "issues" else {},
        hotspots=answers if name == "hotspots" else {},
    )
    assert result.returncode == 1
    assert result.stdout.splitlines() == [
        f"::error::Quality Gate has FAILED (ERROR). Detailed information can be found at: {dashboard}",
        *[f"agentihooks:scripts/page.py:{line} python:S905 Finding {line}" for line in range(1, 102)],
    ]
    requests = _searches(tmp_path, f"{name}/search")
    assert len(requests) == 2
    assert f"&{scope}&" in requests[0]
    assert requests[1] == requests[0] + "&p=2"
    assert result.stderr == ""
    assert (tmp_path / "output").read_text() == ""


@pytest.mark.parametrize("name", ["issues", "hotspots"])
@pytest.mark.parametrize("last_page", [400, {"paging": {"total": 103}}], ids=["refused", "empty"])
@pytest.mark.feature("sonar-failed-gate-lists-every-page")
def test_a_later_finding_page_cannot_lose_printed_findings(
    tmp_path: Path, name: str, last_page: dict[str, object] | int
) -> None:
    first = {
        "component": "agentihooks:scripts/page.py",
        "rule": "python:S905",
        "ruleKey": "python:S905",
        "message": "First page",
    }
    final = {**last_page, name: []} if isinstance(last_page, dict) else last_page
    pages = [{name: [first], "total": 103, "paging": {"total": 103}}, final]
    answers = cast(list[dict[str, object] | int], pages)
    result, _ = _gate(
        tmp_path,
        ["SUCCESS"],
        "ERROR",
        dashboard=_BRANCH,
        issues=answers if name == "issues" else {},
        hotspots=answers if name == "hotspots" else {},
    )
    assert result.returncode == 1
    suffix = "hotspots " if name == "hotspots" else ""
    assert result.stdout.splitlines() == [
        _FAILED,
        "agentihooks:scripts/page.py:0 python:S905 First page",
        f"102 more {suffix}not shown",
    ]
    assert len(_searches(tmp_path, f"{name}/search")) == 2
    assert result.stderr == ""
    assert not (tmp_path / "summary").exists()
    assert (tmp_path / "output").read_text() == ""


@pytest.mark.feature("sonar-failed-gate-lists-every-page")
def test_all_finding_pages_share_one_diagnostic_timeout(tmp_path: Path) -> None:
    result, _ = _gate(tmp_path, ["SUCCESS"], "ERROR", dashboard=_BRANCH, timeout=124)
    assert result.returncode == 1
    assert result.stdout.splitlines() == [_FAILED]
    assert result.stderr == ""
    assert not (tmp_path / "summary").exists()
    assert (tmp_path / "output").read_text() == ""
    arguments = (tmp_path / "timeout").read_text().splitlines()
    assert arguments.count("timeout") == 1
    assert "30s" in arguments
    assert _searches(tmp_path) == []
    assert _searches(tmp_path, "hotspots/search") == []


def _scanner_zip(path):
    with zipfile.ZipFile(path, "w") as archive:
        info = zipfile.ZipInfo(f"sonar-scanner-{_VERSION}-linux-x64/bin/sonar-scanner")
        info.external_attr = 0o755 << 16
        archive.writestr(info, '#!/usr/bin/env bash\necho "$0 $*" > "$SCAN_LOG"\n')


def _scan(tmp_path, cached, partial=False, checksum=None, node=""):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    tool_cache = tmp_path / "toolcache"
    scanner = tool_cache / "sonar-scanner-cli" / _VERSION / "linux-x64"
    if cached:
        (scanner / "bin").mkdir(parents=True)
        _tool(scanner / "bin", "sonar-scanner", 'echo "$0 $*" > "$SCAN_LOG"')
    if partial:
        scanner.mkdir(parents=True)
    archive = tmp_path / "scanner.zip"
    _scanner_zip(archive)
    _tool(
        bin_dir,
        "curl",
        f'echo "$*" >> "{tmp_path}/downloads"\n'
        'while [[ $# -gt 0 ]]; do [[ "$1" == -o ]] && out="$2"; shift; done\n'
        f'cp "{archive}" "$out"',
    )
    runner_temp = tmp_path / "temp"
    runner_temp.mkdir()
    result = _run(
        _step("SonarQube Scan"),
        tmp_path,
        bin_dir,
        RUNNER_TOOL_CACHE=str(tool_cache),
        RUNNER_TEMP=str(runner_temp),
        SONAR_SCANNER_VERSION=_VERSION,
        SONAR_SCANNER_SHA256=checksum or hashlib.sha256(archive.read_bytes()).hexdigest(),
        ARGS="-Dsonar.pullrequest.key=7 -Dsonar.pullrequest.base=dev",
        SCAN_LOG=str(tmp_path / "scan"),
        SONAR_NODE=node,
    )
    downloads = (tmp_path / "downloads").read_text() if (tmp_path / "downloads").exists() else ""
    return result, scanner, downloads


def test_a_restored_scanner_runs_without_a_download(tmp_path):
    result, scanner, downloads = _scan(tmp_path, cached=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert downloads == ""
    quota = subprocess.run(
        ["python", str(_ROOT / "scripts/ci_cpu_quota.py")], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert (tmp_path / "scan").read_text().split() == [
        f"{scanner}/bin/sonar-scanner",
        "-Dsonar.projectBaseDir=.",
        f"-Dsonar.python.analysis.threads={quota}",
        "-Dsonar.pullrequest.key=7",
        "-Dsonar.pullrequest.base=dev",
    ]


def test_the_scanner_heap_is_raised_and_its_pauses_logged():
    opts = _step("SonarQube Scan")["env"]["SONAR_SCANNER_JAVA_OPTS"].split()
    assert "-Xmx3g" in opts
    assert any(opt.startswith("-Xlog:gc:file=") for opt in opts)


def test_a_missing_scanner_is_downloaded_into_the_cached_path(tmp_path):
    result, scanner, downloads = _scan(tmp_path, cached=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"sonar-scanner-cli/sonar-scanner-cli-{_VERSION}-linux-x64.zip" in downloads
    assert (tmp_path / "scan").read_text().split()[0] == f"{scanner}/bin/sonar-scanner"


def test_a_partly_restored_scanner_is_completed_in_place(tmp_path):
    result, scanner, downloads = _scan(tmp_path, cached=False, partial=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (tmp_path / "scan").read_text().split()[0] == f"{scanner}/bin/sonar-scanner"


def test_a_scanner_archive_with_another_checksum_never_runs(tmp_path):
    result, scanner, _ = _scan(tmp_path, cached=False, checksum="0" * 64)
    assert result.returncode != 0
    assert not (tmp_path / "scan").exists()
    assert not (scanner / "bin").exists()


def test_the_pinned_checksum_tracks_the_scanner_version():
    env = _sonar()["env"]
    assert env["SONAR_SCANNER_VERSION"] == _VERSION
    assert env["SONAR_SCANNER_SHA256"] == "da9f4e64a3d555f08ce38b5469ebd91fe2b311af473f7001a5ee5c1fd58b004b"


def test_the_runner_image_node_is_handed_to_sonarjs(tmp_path):
    node = tmp_path / "node"
    node.write_text("#!/usr/bin/env bash\n")
    node.chmod(0o755)
    result, _, downloads = _scan(tmp_path, cached=True, node=str(node))
    assert result.returncode == 0, result.stdout + result.stderr
    assert downloads == ""
    assert f"-Dsonar.nodejs.executable={node}" in (tmp_path / "scan").read_text().split()


def test_a_runner_without_the_image_node_lets_sonarjs_use_its_own(tmp_path):
    result, _, _ = _scan(tmp_path, cached=True, node=str(tmp_path / "missing"))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "sonar.nodejs.executable" not in (tmp_path / "scan").read_text()


def _merge_tree(tmp_path, combine_body):
    tree = tmp_path / "tree"
    (tree / ".github/coverage").mkdir(parents=True)
    (tree / ".github/coverage/combine.sh").write_text(combine_body)
    temp = tmp_path / "temp"
    temp.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    return tree, temp, bin_dir


def _start_merge(tree, temp, bin_dir):
    output = temp / "output"
    unit = yaml.safe_load((_ROOT / ".github/workflows/test.yml").read_text())["jobs"]["unit"]
    shards = str(unit["strategy"]["matrix"]["shard"][-1])
    started = _run(
        _step("Merge shard coverage"), tree, bin_dir, RUNNER_TEMP=str(temp), GITHUB_OUTPUT=str(output), SHARDS=shards
    )
    assert started.returncode == 0, started.stdout + started.stderr
    return output.read_text().strip().removeprefix("pid=")


def _wait_merge(tree, temp, bin_dir, pid):
    return _run(_step("Wait for the coverage merge"), tree, bin_dir, RUNNER_TEMP=str(temp), MERGE_PID=pid)


def test_the_coverage_merge_runs_behind_the_setup_and_the_scan_waits_for_it(tmp_path):
    release = tmp_path / "release"
    tree, temp, bin_dir = _merge_tree(tmp_path, f'until [[ -e "{release}" ]]; do sleep 0.05; done\necho "merged $*"\n')
    pid = _start_merge(tree, temp, bin_dir)
    assert pid.isdigit()
    assert not (temp / "combine.status").exists()
    release.touch()
    waited = _wait_merge(tree, temp, bin_dir, pid)
    assert waited.returncode == 0, waited.stdout + waited.stderr
    unit = yaml.safe_load((_ROOT / ".github/workflows/test.yml").read_text())["jobs"]["unit"]
    assert f"merged --downloaded {unit['strategy']['matrix']['shard'][-1]}" in waited.stdout


def test_a_failed_background_merge_fails_the_wait(tmp_path):
    tree, temp, bin_dir = _merge_tree(tmp_path, 'echo "::error::Missing coverage for shard 3"\nexit 3\n')
    waited = _wait_merge(tree, temp, bin_dir, _start_merge(tree, temp, bin_dir))
    assert waited.returncode == 3
    assert "Missing coverage for shard 3" in waited.stdout


@pytest.mark.parametrize("pid", ["", "999999999"], ids=["merge-never-started", "merge-died"])
def test_a_merge_that_leaves_no_status_fails_the_wait_at_once(tmp_path, pid):
    tree, temp, bin_dir = _merge_tree(tmp_path, "")
    waited = _wait_merge(tree, temp, bin_dir, pid)
    assert waited.returncode == 1
    assert "::error::The coverage merge ended without a status." in waited.stdout


def test_setup_and_evidence_upload_overlap_existing_waits() -> None:
    steps = _sonar()["steps"]
    names = [step.get("name") or step.get("uses") for step in steps]
    order = [
        "Take each shard's coverage as it lands",
        "Merge shard coverage",
        "Start Cloudflare Access proxy",
        "Wait for the coverage merge",
        "SonarQube Scan",
        "Upload coverage and analysis evidence",
        "SonarQube Quality Gate",
        "Hold the Delivery L2 conditions",
    ]
    assert [names.index(name) for name in order] == sorted(names.index(name) for name in order)
    assert "Check dev still points at this commit" not in names
    wait = _step("Wait for the coverage merge")
    assert wait["if"] == "always() && needs.select.outputs.scope != 'none'"
    assert wait["timeout-minutes"] == 5


def test_sonar_evidence_does_not_upload_shard_databases_twice():
    upload = _step("Upload coverage and analysis evidence")
    reports = ["coverage.xml", "lcov.info", ".scannerwork/report-task.txt"]
    assert upload["with"]["path"].splitlines() in (reports, ["sonar-scope.txt", *reports])
    assert upload["if"] == "always()"
    assert upload["with"]["if-no-files-found"] == "error"
    unit = yaml.safe_load((_ROOT / ".github/workflows/test.yml").read_text())["jobs"]["unit"]
    shards = next(step for step in unit["steps"] if step.get("name") == "Upload coverage")
    assert shards["with"]["path"].splitlines() == [".coverage", "js-coverage/", "dep-inputs/", "reused-coverage/"]
    assert shards["with"]["include-hidden-files"] is True
