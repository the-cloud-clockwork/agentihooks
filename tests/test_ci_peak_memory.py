import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]
PEAK = ROOT / ".github/scripts/peak_memory.py"
WORKFLOW = ROOT / ".github/workflows/test.yml"
ALLOCATE = "import sys; block = bytearray(160 * 1024 * 1024); block[::4096] = b'x' * len(block[::4096]); sys.exit(3)"


def _run(tmp_path: Path, *command: str) -> subprocess.CompletedProcess[str]:
    summary = tmp_path / "summary.md"
    env = {"PATH": "/usr/bin:/bin", "PEAK_MEMORY_DIR": str(tmp_path / "peak"), "GITHUB_STEP_SUMMARY": str(summary)}
    return subprocess.run([sys.executable, str(PEAK), *command], env=env, capture_output=True, text=True, check=False)


def test_records_the_peak_of_the_command_and_keeps_its_status(tmp_path: Path) -> None:
    done = _run(tmp_path, "select", "--", sys.executable, "-c", ALLOCATE)
    assert done.returncode == 3
    entry = json.loads((tmp_path / "peak/select.json").read_text())
    assert entry["label"] == "select"
    assert entry["exit"] == 3
    assert entry["peak_rss_kib"] >= 160 * 1024
    assert entry["peak_rss_mib"] == round(entry["peak_rss_kib"] / 1024, 1)
    line = f"Peak resident memory of select: {entry['peak_rss_mib']} MiB"
    assert (tmp_path / "summary.md").read_text() == line + "\n"
    assert done.stderr.strip() == line
    assert done.stdout == ""


def test_passes_the_command_output_through(tmp_path: Path) -> None:
    done = _run(tmp_path, "plan", "--", sys.executable, "-c", "print('scope=selected')")
    assert done.returncode == 0
    assert done.stdout == "scope=selected\n"


def test_a_signalled_command_exits_as_the_shell_reports_it(tmp_path: Path) -> None:
    done = _run(
        tmp_path, "killed", "--", sys.executable, "-c", "import os, signal; os.kill(os.getpid(), signal.SIGKILL)"
    )
    assert done.returncode == 137
    assert json.loads((tmp_path / "peak/killed.json").read_text())["exit"] == 137


@pytest.mark.parametrize("arguments", [[], ["select"], ["select", "true"], ["select", "--"]])
def test_refuses_a_call_without_label_and_command(tmp_path: Path, arguments: list[str]) -> None:
    done = _run(tmp_path, *arguments)
    assert done.returncode == 2
    assert not (tmp_path / "peak").exists()


@pytest.mark.parametrize(
    ("job", "step", "label", "helper"),
    [
        (
            "select",
            "Select the tests the change reaches",
            "select",
            '"$GITHUB_WORKSPACE/.github/scripts/peak_memory.py"',
        ),
        (
            "mutation-plan",
            "Size mutation shards from stored timings",
            "mutation-plan",
            '"$RUNNER_TEMP/workflow/.github/scripts/peak_memory.py"',
        ),
    ],
)
def test_pull_request_tests_record_and_upload_the_step_peak(job: str, step: str, label: str, helper: str) -> None:
    steps = yaml.safe_load(WORKFLOW.read_text())["jobs"][job]["steps"]
    names = [entry.get("name") for entry in steps]
    measured = steps[names.index(step)]
    assert f"{helper} {label} -- " in measured["run"]
    upload = steps[names.index(f"Upload the {label} step peak memory")]
    assert names.index(step) < names.index(upload["name"])
    assert upload["if"].startswith("always() && github.event_name == 'pull_request'")
    assert upload["with"]["name"] == f"peak-memory-{label}"
    assert upload["with"]["path"] == f"${{{{ runner.temp }}}}/peak-memory/{label}.json"
    assert upload["with"]["after-cancel"] is True
