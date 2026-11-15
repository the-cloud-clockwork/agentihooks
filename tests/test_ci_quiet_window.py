import os
import subprocess
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[1]


def _workflow() -> dict:
    """Return the parsed Tests workflow."""
    return yaml.safe_load((_ROOT / ".github/workflows/test.yml").read_text())


def _quiet_window_step() -> dict:
    """Return the select job step that holds the heavy legs while the head moves."""
    steps = _workflow()["jobs"]["select"]["steps"]
    return next(s for s in steps if s.get("name") == "Hold the heavy legs until the head stops moving")


def _run_quiet_window(tmp_path: Path, quiet: str, heads: list[str], fake_sleep: bool) -> tuple[int, int]:
    """Run the quiet window step against a fake remote and return its exit code and remote reads."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "calls"
    calls.write_text("")
    git = bin_dir / "git"
    git.write_text(
        "#!/usr/bin/env bash\n"
        '[[ "$*" == *"ls-remote origin refs/pull/7/head"* ]] || exit 9\n'
        f'echo x >> "{calls}"\n'
        f'n=$(wc -l < "{calls}")\n'
        f"heads=({' '.join(heads)})\n"
        'head="${heads[$(( n > ${#heads[@]} ? ${#heads[@]} - 1 : n - 1 ))]}"\n'
        '[[ "$head" != fail ]] || exit 3\n'
        'printf "%s\\trefs/pull/7/head\\n" "$head"\n'
    )
    git.chmod(0o755)
    if fake_sleep:
        sleep = bin_dir / "sleep"
        sleep.write_text("#!/usr/bin/env bash\n")
        sleep.chmod(0o755)
    env = dict(
        os.environ,
        PATH=f"{bin_dir}:{os.environ['PATH']}",
        GITHUB_WORKSPACE=str(tmp_path),
        QUIET=quiet,
        PULL="7",
        HEAD_SHA="a" * 40,
    )
    step = _quiet_window_step()
    result = subprocess.run(["bash", "-eo", "pipefail", "-c", step["run"]], env=env, capture_output=True, text=True)
    return result.returncode, len(calls.read_text().splitlines())


def test_quiet_window_holds_select_on_pull_requests_before_the_selection() -> None:
    steps = _workflow()["jobs"]["select"]["steps"]
    step = _quiet_window_step()
    select = next(s for s in steps if s.get("id") == "select")
    assert steps.index(step) == steps.index(select) - 1
    assert step["if"] == "github.event_name == 'pull_request' && vars.AGENTIHOOKS_CI_QUIET_SECONDS != ''"
    assert step["env"] == {
        "QUIET": "${{ vars.AGENTIHOOKS_CI_QUIET_SECONDS }}",
        "PULL": "${{ github.event.pull_request.number }}",
        "HEAD_SHA": "${{ github.event.pull_request.head.sha }}",
    }
    assert "gh " not in step["run"]
    assert "continue-on-error" not in step


def test_quiet_window_stops_the_heavy_legs_when_the_head_moves(tmp_path: Path) -> None:
    code, reads = _run_quiet_window(tmp_path, "30", ["a" * 40, "b" * 40], fake_sleep=True)
    assert code == 1
    assert reads == 2


def test_quiet_window_releases_an_unchanged_head_at_the_deadline(tmp_path: Path) -> None:
    code, reads = _run_quiet_window(tmp_path, "1", ["a" * 40], fake_sleep=False)
    assert code == 0
    assert reads == 1


def test_quiet_window_of_zero_reads_nothing(tmp_path: Path) -> None:
    assert _run_quiet_window(tmp_path, "0", ["b" * 40], fake_sleep=True) == (0, 0)


@pytest.mark.parametrize("quiet", ["1m", "010", "61", "100", "-1", ""])
def test_quiet_window_refuses_a_value_outside_whole_seconds_up_to_60(tmp_path: Path, quiet: str) -> None:
    assert _run_quiet_window(tmp_path, quiet, ["a" * 40], fake_sleep=True) == (1, 0)


def test_quiet_window_fails_when_the_head_cannot_be_read(tmp_path: Path) -> None:
    assert _run_quiet_window(tmp_path, "30", ["a" * 40, "fail"], fake_sleep=True) == (1, 2)
