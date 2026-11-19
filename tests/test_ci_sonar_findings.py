import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]


def _gate(
    tmp_path: Path, gate: str = "ERROR", scope: str = "pullRequest=17", empty: bool = False, failure: str = ""
) -> subprocess.CompletedProcess[str]:
    (tmp_path / ".scannerwork").mkdir()
    (tmp_path / ".scannerwork/report-task.txt").write_text(
        f"projectKey=agentihooks\nceTaskId=T1\ndashboardUrl=http://localhost:9000/dashboard?id=agentihooks&{scope}\n"
    )
    (tmp_path / "fixtures.json").write_text(
        json.dumps({"gate": gate, "scope": scope, "empty": empty, "failure": failure})
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    curl = bin_dir / "curl"
    curl.write_text(
        "#!/usr/bin/env python3\n"
        "import json, pathlib, sys\n"
        "from urllib.parse import parse_qs, urlsplit\n"
        "root = pathlib.Path.cwd()\n"
        "fixture = json.loads((root / 'fixtures.json').read_text())\n"
        "url = urlsplit(sys.argv[-1])\n"
        "assert url.scheme == 'http' and url.netloc == '127.0.0.1:9000'\n"
        "params = parse_qs(url.query)\n"
        "with (root / 'requests').open('a') as stream:\n"
        "    stream.write(json.dumps({'path': url.path, 'params': params}) + '\\n')\n"
        "if url.path == '/api/ce/task':\n"
        "    assert params == {'id': ['T1']}\n"
        "    answer = {'task': {'status': 'SUCCESS', 'analysisId': 'A1'}}\n"
        "elif url.path == '/api/qualitygates/project_status':\n"
        "    assert params == {'analysisId': ['A1']}\n"
        "    answer = {'projectStatus': {'status': fixture['gate']}}\n"
        "else:\n"
        "    kind = url.path.split('/')[2]\n"
        "    assert kind in ('issues', 'hotspots') and url.path.endswith('/search')\n"
        "    assert 'Authorization: Bearer read' in sys.argv\n"
        "    page = int(params['p'][0])\n"
        "    expected = dict(parse_qs(fixture['scope']), inNewCodePeriod=['true'], p=[str(page)])\n"
        "    if kind == 'issues':\n"
        "        expected.update(components=['agentihooks'], resolved=['false'], ps=['500'])\n"
        "    else:\n"
        "        expected.update(project=['agentihooks'], ps=['100'])\n"
        "    assert params == expected, params\n"
        "    if fixture['failure'] == kind:\n"
        "        print('diagnostic endpoint refused', file=sys.stderr)\n"
        "        sys.exit(22)\n"
        "    assert page in (1, 2)\n"
        "    item = {'component': 'agentihooks:source', 'message': f'{kind} message {page}'}\n"
        "    if kind == 'issues':\n"
        "        item['rule'] = f'python:S{page}'\n"
        "    else:\n"
        "        item['ruleKey'] = f'python:S{page + 2}'\n"
        "    if page == 1:\n"
        "        item['line'] = 12 if kind == 'issues' else 34\n"
        "    answer = {'paging': {'pageIndex': page, 'pageSize': 1, 'total': 0 if fixture['empty'] else 2},\n"
        "              kind: [] if fixture['empty'] else [item],\n"
        "              'components': [{'key': 'agentihooks:source', 'path': 'source with spaces.py'}]}\n"
        "print(json.dumps(answer))\n"
    )
    curl.chmod(0o755)
    workflow = yaml.safe_load((ROOT / ".github/workflows/test.yml").read_text())
    step = next(step for step in workflow["jobs"]["sonar"]["steps"] if step.get("name") == "SonarQube Quality Gate")
    return subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", step["run"]],
        cwd=tmp_path,
        env={
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "GITHUB_WORKSPACE": str(ROOT),
            "GITHUB_EVENT_NAME": "pull_request",
            "GITHUB_OUTPUT": str(tmp_path / "output"),
            "SONAR_TEST_ONLY": "false",
            "SONAR_TOKEN": "scan",
            "SONAR_READ_TOKEN": "read",
        },
        capture_output=True,
        text=True,
        timeout=15,
    )


@pytest.mark.parametrize("scope", ["pullRequest=17", "pullRequest=dispatch-a%2Fb", "branch=dev"])
def test_a_failed_gate_prints_every_new_issue_and_hotspot(tmp_path: Path, scope: str) -> None:
    result = _gate(tmp_path, scope=scope)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "Quality Gate has FAILED" in result.stdout
    assert "New issues: 2" in result.stdout
    assert "New hotspots: 2" in result.stdout
    for rule, line, message in (
        (1, "12", "issues message 1"),
        (2, "-", "issues message 2"),
        (3, "34", "hotspots message 1"),
        (4, "-", "hotspots message 2"),
    ):
        assert f"rule=python:S{rule} file=source with spaces.py line={line} message={message}" in result.stdout
    requests = [json.loads(line) for line in (tmp_path / "requests").read_text().splitlines()]
    assert len(requests) == 6
    assert not (tmp_path / "output").exists()


def test_a_passed_gate_never_requests_findings(tmp_path: Path) -> None:
    result = _gate(tmp_path, gate="OK")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Quality Gate has PASSED" in result.stdout
    assert (tmp_path / "output").read_text() == "analysis=A1\n"
    assert len((tmp_path / "requests").read_text().splitlines()) == 2


def test_a_failed_gate_without_findings_still_reports_zero_and_fails(tmp_path: Path) -> None:
    result = _gate(tmp_path, empty=True)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "Quality Gate has FAILED" in result.stdout
    assert "New issues: 0" in result.stdout
    assert "New hotspots: 0" in result.stdout
    assert len((tmp_path / "requests").read_text().splitlines()) == 4
    assert not (tmp_path / "output").exists()


@pytest.mark.parametrize("failure", ["issues", "hotspots"])
def test_a_failed_findings_request_keeps_the_gate_red(tmp_path: Path, failure: str) -> None:
    result = _gate(tmp_path, failure=failure)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "Quality Gate has FAILED" in result.stdout
    assert "Could not list new Sonar findings" in result.stdout
    assert "diagnostic endpoint refused" in result.stderr
    assert not (tmp_path / "output").exists()
