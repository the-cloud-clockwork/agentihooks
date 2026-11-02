import json
import re
import signal
import subprocess
import sys
import time
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


@pytest.mark.parametrize(("sent", "status"), [(signal.SIGINT, 130), (signal.SIGTERM, 143)])
def test_a_cancelled_step_forwards_the_signal_and_still_records(tmp_path: Path, sent: int, status: int) -> None:
    ready = tmp_path / "ready"
    child = f"import pathlib, time; pathlib.Path({str(ready)!r}).touch(); time.sleep(60)"
    env = {"PATH": "/usr/bin:/bin", "PEAK_MEMORY_DIR": str(tmp_path / "peak")}
    wrapper = subprocess.Popen([sys.executable, str(PEAK), "cancelled", "--", sys.executable, "-c", child], env=env)
    deadline = time.monotonic() + 30
    while not ready.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    wrapper.send_signal(sent)
    assert wrapper.wait(timeout=30) == status
    assert json.loads((tmp_path / "peak/cancelled.json").read_text())["exit"] == status


@pytest.mark.parametrize("arguments", [[], ["select"], ["select", "true"], ["select", "--"]])
def test_refuses_a_call_without_label_and_command(tmp_path: Path, arguments: list[str]) -> None:
    done = _run(tmp_path, *arguments)
    assert done.returncode == 2
    assert not (tmp_path / "peak").exists()


@pytest.mark.parametrize(
    ("job", "step", "label", "helper", "action", "guard"),
    [
        (
            "select",
            "Select the tests the change reaches",
            "select",
            '-I "$RUNNER_TEMP/grader/.github/scripts/peak_memory.py"',
            "./.github/actions/retry-upload",
            "'pull_request'",
        ),
        (
            "mutation-plan",
            "Size mutation shards from stored timings",
            "mutation-plan",
            '-I "$RUNNER_TEMP/workflow/.github/scripts/peak_memory.py"',
            "./.workflow-revision/.github/actions/retry-upload",
            "steps.reuse.outputs.reused != 'true'",
        ),
    ],
)
def test_pull_request_tests_record_and_upload_the_step_peak(
    job: str, step: str, label: str, helper: str, action: str, guard: str
) -> None:
    steps = yaml.safe_load(WORKFLOW.read_text())["jobs"][job]["steps"]
    names = [entry.get("name") for entry in steps]
    measured = steps[names.index(step)]
    assert f"{helper} {label} --" in measured["run"]
    assert "$GITHUB_WORKSPACE/.github/scripts/peak_memory.py" not in measured["run"]
    upload = steps[names.index(f"Upload the {label} step peak memory")]
    assert names.index(step) < names.index(upload["name"])
    assert upload["if"].startswith("always() && github.event_name == 'pull_request'")
    assert upload["with"]["name"] == f"peak-memory-{label}"
    assert upload["with"]["path"] == f"${{{{ runner.temp }}}}/peak-memory/{label}.json"
    assert upload["with"]["after-cancel"] is True
    assert upload["uses"] == action
    assert upload["if"].endswith(guard)
    if job == "mutation-plan":
        assert measured["if"] == "steps.reuse.outputs.reused != 'true'"
        extract = names.index("Take the helper scripts from the workflow revision")
        assert extract < names.index(step)
        assert steps[extract]["if"] == measured["if"]


LIGHT_CANONICAL = {"admission", "reuse"}


def test_every_light_runner_job_records_its_whole_job_peak_memory() -> None:
    jobs = yaml.safe_load(WORKFLOW.read_text())["jobs"]
    light = [name for name, job in jobs.items() if "'anton-oss-light'" in str(job.get("runs-on", ""))]
    measured = [name for name in light if name not in LIGHT_CANONICAL]
    assert {"select", "shard-check", "mutation-plan", "mutation-refine", "mutation"} <= set(measured)
    for name in measured:
        last = jobs[name]["steps"][-1]
        assert last["name"] == "Record the job peak memory", name
        assert last["if"] == "always()"
        assert last["shell"] == "bash"
        assert "memory.peak memory.max_usage_in_bytes" in last["run"]
        assert "memory.max memory.limit_in_bytes" in last["run"]
        assert '"oom_kill"' in last["run"]
        assert 'tee -a "$GITHUB_STEP_SUMMARY"' in last["run"]


def test_the_job_peak_step_reports_the_cgroup_figures(tmp_path: Path) -> None:
    run = yaml.safe_load(WORKFLOW.read_text())["jobs"]["select"]["steps"][-1]["run"]
    summary = tmp_path / "summary.md"
    env = {"PATH": "/usr/bin:/bin", "GITHUB_JOB": "select", "GITHUB_STEP_SUMMARY": str(summary)}
    done = subprocess.run(["bash", "-e", "-c", run], env=env, capture_output=True, text=True, check=False)
    assert done.returncode == 0
    figure = r"(\d+ MiB|unknown)"
    pattern = rf"Job peak memory of select: {figure}, limit (\d+ MiB|max|unknown), out of memory kills (\d+|unknown)"
    assert re.fullmatch(pattern, done.stdout.strip())
    assert summary.read_text() == done.stdout
