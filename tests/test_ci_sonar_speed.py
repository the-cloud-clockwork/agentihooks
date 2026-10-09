import hashlib
import os
import subprocess
import zipfile
from pathlib import Path

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
        env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", **env},
        capture_output=True,
        text=True,
        timeout=30,
    )


def _gate(tmp_path, statuses, gate="OK"):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (tmp_path / ".scannerwork").mkdir()
    (tmp_path / ".scannerwork/report-task.txt").write_text("ceTaskId=T1\ndashboardUrl=http://sonar/dash\n")
    (tmp_path / "statuses").write_text("\n".join(statuses) + "\n")
    _tool(
        bin_dir,
        "curl",
        f'url="${{@: -1}}"; echo "$url" >> "{tmp_path}/urls"\n'
        'case "$url" in\n'
        f'  */ce/task?id=T1) s=$(head -n1 "{tmp_path}/statuses"); sed -i 1d "{tmp_path}/statuses"\n'
        '    echo "{\\"task\\":{\\"status\\":\\"$s\\",\\"analysisId\\":\\"A1\\"}}" ;;\n'
        f'  */qualitygates/project_status?analysisId=A1) echo \'{{"projectStatus":{{"status":"{gate}"}}}}\' ;;\n'
        "  *) exit 22 ;;\n"
        "esac",
    )
    _tool(bin_dir, "sleep", f'echo "$1" >> "{tmp_path}/sleeps"')
    result = _run(_step("SonarQube Quality Gate"), tmp_path, bin_dir, SONAR_TOKEN="t")
    sleeps = (tmp_path / "sleeps").read_text().split() if (tmp_path / "sleeps").exists() else []
    return result, sleeps


def test_the_quality_gate_is_polled_every_second_until_the_analysis_is_processed(tmp_path):
    result, sleeps = _gate(tmp_path, ["PENDING", "IN_PROGRESS", "SUCCESS"])
    assert result.returncode == 0, result.stdout + result.stderr
    assert sleeps == ["1", "1"]
    assert "Quality Gate has PASSED" in result.stdout


@pytest.mark.parametrize(("statuses", "gate"), [(["SUCCESS"], "ERROR"), (["FAILED"], "OK"), (["CANCELED"], "OK")])
def test_a_failed_gate_or_background_task_is_red(tmp_path, statuses, gate):
    result, _ = _gate(tmp_path, statuses, gate)
    assert result.returncode != 0
    assert "::error::" in result.stdout


def test_the_gate_step_replaces_the_quality_gate_action():
    sonar = _sonar()
    assert not any("sonarqube-quality-gate-action" in step.get("uses", "") for step in sonar["steps"])
    gate = _step("SonarQube Quality Gate")
    assert gate["if"] == "steps.current.outputs.superseded != 'true'"
    assert gate["timeout-minutes"] == 5
    assert not gate.get("continue-on-error")


def _scanner_zip(path):
    with zipfile.ZipFile(path, "w") as archive:
        info = zipfile.ZipInfo(f"sonar-scanner-{_VERSION}-linux-x64/bin/sonar-scanner")
        info.external_attr = 0o755 << 16
        archive.writestr(info, '#!/usr/bin/env bash\necho "$0 $*" > "$SCAN_LOG"\n')


def _scan(tmp_path, cached, partial=False, checksum=None):
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
    )
    downloads = (tmp_path / "downloads").read_text() if (tmp_path / "downloads").exists() else ""
    return result, scanner, downloads


def test_a_restored_scanner_runs_without_a_download(tmp_path):
    result, scanner, downloads = _scan(tmp_path, cached=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert downloads == ""
    assert (tmp_path / "scan").read_text().split() == [
        f"{scanner}/bin/sonar-scanner",
        "-Dsonar.projectBaseDir=.",
        "-Dsonar.pullrequest.key=7",
        "-Dsonar.pullrequest.base=dev",
    ]


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


def test_the_node_runtime_has_its_own_cache_so_the_downloads_entry_keeps_its_version():
    names = [step.get("name") for step in _sonar()["steps"]]
    node = _step("Restore the SonarJS Node runtime")
    assert node["uses"] == "actions/cache@v4"
    assert node["with"]["path"] == "~/.sonar/js/node-runtime"
    assert "${{ steps.proxy.outputs.version }}" in node["with"]["key"]
    assert "if" not in node
    assert names.index("Start Cloudflare Access proxy") < names.index("Restore the SonarJS Node runtime")
    assert names.index("Restore the SonarJS Node runtime") < names.index("SonarQube Scan")
    assert "node-runtime" not in _step("Restore Sonar downloads")["with"]["path"]


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
    started = _run(_step("Merge shard coverage"), tree, bin_dir, RUNNER_TEMP=str(temp), GITHUB_OUTPUT=str(output))
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
    assert "merged --downloaded 8" in waited.stdout


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


def test_setup_and_evidence_upload_overlap_existing_waits():
    steps = _sonar()["steps"]
    names = [step.get("name") or step.get("uses") for step in steps]
    order = [
        "Download shard coverage",
        "Merge shard coverage",
        "Start Cloudflare Access proxy",
        "Restore Sonar downloads",
        "Wait for the coverage merge",
        "SonarQube Scan",
        "Upload coverage and analysis evidence",
        "SonarQube Quality Gate",
        "Hold the Delivery L2 conditions",
    ]
    assert [names.index(name) for name in order] == sorted(names.index(name) for name in order)
    assert names.index("Check dev still points at this commit") < names.index("Download shard coverage")
    wait = _step("Wait for the coverage merge")
    assert wait["if"] == "always() && steps.current.outputs.superseded != 'true'"
    assert wait["timeout-minutes"] == 5


def test_sonar_evidence_does_not_upload_shard_databases_twice():
    upload = _step("Upload coverage and analysis evidence")
    assert upload["with"]["path"].splitlines() == [
        "coverage.xml",
        ".scannerwork/report-task.txt",
    ]
    assert upload["if"] == "always() && steps.current.outputs.superseded != 'true'"
    assert upload["with"]["if-no-files-found"] == "error"
    unit = yaml.safe_load((_ROOT / ".github/workflows/test.yml").read_text())["jobs"]["unit"]
    shards = next(step for step in unit["steps"] if step.get("name") == "Upload coverage")
    assert shards["with"]["path"] == ".coverage"
    assert shards["with"]["include-hidden-files"] is True
