import os
import subprocess
from datetime import datetime, timedelta, timezone
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


def _stamp(seconds_ago: int) -> str:
    """Return the UTC second stamp GitHub writes for a pull request change that many seconds before now."""
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _run_quiet_window(
    tmp_path: Path, quiet: str, heads: list[str], fake_sleep: bool, updated_at: str
) -> tuple[int, int]:
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
        UPDATED_AT=updated_at,
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
        "UPDATED_AT": "${{ github.event.pull_request.updated_at }}",
    }
    assert "gh " not in step["run"]
    assert "continue-on-error" not in step


def test_quiet_window_stops_the_heavy_legs_when_the_head_moves(tmp_path: Path) -> None:
    code, reads = _run_quiet_window(tmp_path, "30", ["a" * 40, "b" * 40], fake_sleep=True, updated_at=_stamp(0))
    assert code == 1
    assert reads == 2


def test_quiet_window_releases_an_unchanged_head_at_the_deadline(tmp_path: Path) -> None:
    code, reads = _run_quiet_window(tmp_path, "2", ["a" * 40], fake_sleep=False, updated_at=_stamp(0))
    assert code == 0
    assert reads == 1


def test_quiet_window_of_zero_reads_nothing(tmp_path: Path) -> None:
    assert _run_quiet_window(tmp_path, "0", ["b" * 40], fake_sleep=True, updated_at="") == (0, 0)


@pytest.mark.parametrize("quiet", ["1m", "010", "61", "100", "-1", ""])
def test_quiet_window_refuses_a_value_outside_whole_seconds_up_to_60(tmp_path: Path, quiet: str) -> None:
    assert _run_quiet_window(tmp_path, quiet, ["a" * 40], fake_sleep=True, updated_at=_stamp(0)) == (1, 0)


def test_quiet_window_fails_when_the_head_cannot_be_read(tmp_path: Path) -> None:
    assert _run_quiet_window(tmp_path, "30", ["a" * 40, "fail"], fake_sleep=True, updated_at=_stamp(0)) == (1, 2)


def test_quiet_window_reads_once_and_holds_nothing_after_a_window_since_the_change(tmp_path: Path) -> None:
    assert _run_quiet_window(tmp_path, "60", ["a" * 40], fake_sleep=False, updated_at=_stamp(120)) == (0, 1)


def test_quiet_window_stops_a_moved_head_after_a_window_since_the_change(tmp_path: Path) -> None:
    assert _run_quiet_window(tmp_path, "60", ["b" * 40], fake_sleep=True, updated_at=_stamp(120)) == (1, 1)


def test_quiet_window_holds_only_the_rest_of_the_window_since_the_pull_request_changed(tmp_path: Path) -> None:
    assert _run_quiet_window(tmp_path, "60", ["a" * 40, "b" * 40], fake_sleep=False, updated_at=_stamp(57)) == (0, 1)


def test_quiet_window_holds_at_most_the_window_for_a_change_stamped_ahead_of_the_runner_clock(tmp_path: Path) -> None:
    assert _run_quiet_window(tmp_path, "2", ["a" * 40], fake_sleep=False, updated_at=_stamp(-60)) == (0, 1)


@pytest.mark.parametrize(
    "updated_at",
    ["", "soon", "now", "@0", "1 minute ago", "2026-11-17 11:41:14", "2026-11-17T11:41:14", "2026-13-45T25:61:61Z"],
)
def test_quiet_window_refuses_an_unreadable_change_time(tmp_path: Path, updated_at: str) -> None:
    assert _run_quiet_window(tmp_path, "30", ["a" * 40], fake_sleep=True, updated_at=updated_at) == (1, 0)
