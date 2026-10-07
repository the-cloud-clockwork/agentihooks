import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]


def test_sonar_uses_all_shards_without_running_tests_again():
    workflow = yaml.safe_load((ROOT / ".github/workflows/test.yml").read_text())
    jobs = workflow["jobs"]
    scan = jobs["sonar"]
    assert scan["needs"] == ["unit"]
    assert "sonar-reusable.yml" in scan["uses"]
    assert "pytest" not in scan["with"]["test_command"]
    assert "combine.sh" in scan["with"]["test_command"]
    assert "sonar" not in jobs["gate-required"]["needs"]
    assert not (ROOT / ".github/workflows/sonar-scan.yml").exists()


def test_coverage_options_measure_hooks_and_scripts_on_one_interpreter():
    workflow = yaml.safe_load((ROOT / ".github/workflows/test.yml").read_text())
    steps = workflow["jobs"]["unit"]["steps"]
    run = next(step for step in steps if step.get("name") == "Run tests")
    options = run["env"]["PYTEST_ADDOPTS"]
    assert "matrix.python-version == '3.12'" in options
    assert "--cov=hooks" in options
    assert "--cov=scripts" in options
    assert "-p no:pytest_cov" not in run["run"]
    upload = next(step for step in steps if step.get("name") == "Upload coverage")
    assert "matrix.python-version == '3.12'" in upload["if"]
    assert upload["with"]["include-hidden-files"] is True
    assert upload["with"]["if-no-files-found"] == "error"
    properties = (ROOT / "sonar-project.properties").read_text()
    assert "sonar.coverage.exclusions=scripts/**" not in properties


def test_missing_shard_coverage_is_red(tmp_path):
    for shard in (1, 2, 3):
        folder = tmp_path / ".coverage-shards" / f"coverage-3.12-{shard}"
        folder.mkdir(parents=True)
        (folder / ".coverage").write_text("plant")
    result = subprocess.run(
        ["bash", str(ROOT / ".github/coverage/combine.sh"), "--downloaded", "4"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "Missing coverage for shard 4" in result.stdout


def test_combined_coverage_keeps_hits_from_every_shard_and_both_packages(tmp_path):
    for package in ("hooks", "scripts"):
        (tmp_path / package).mkdir()
        for shard in range(1, 5):
            (tmp_path / package / f"part_{shard}.py").write_text("value = 1\n")
    generator = """
import runpy
import sys
from coverage import Coverage
from pathlib import Path
for shard in range(1, 5):
    folder = Path('.coverage-shards') / f'coverage-3.12-{shard}'
    folder.mkdir(parents=True)
    cov = Coverage(data_file=str(folder / '.coverage'), config_file=sys.argv[1])
    cov.start()
    for package in ('hooks', 'scripts'):
        runpy.run_path(f'{package}/part_{shard}.py')
    cov.stop()
    cov.save()
"""
    subprocess.run(
        [sys.executable, "-c", generator, str(ROOT / ".github/coverage/coverage.ini")],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    result = subprocess.run(
        ["bash", str(ROOT / ".github/coverage/combine.sh"), "--downloaded", "4"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    report = ET.parse(tmp_path / "coverage.xml")
    classes = report.findall(".//class")
    assert {node.attrib["filename"] for node in classes} == {
        f"{package}/part_{shard}.py" for package in ("hooks", "scripts") for shard in range(1, 5)
    }
    assert all(int(line.attrib["hits"]) > 0 for line in report.findall(".//line"))
