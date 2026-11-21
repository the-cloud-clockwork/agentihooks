import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
ACTION = ROOT / ".github/actions/browser-cache"


READY = "steps.launch.outputs.ready != 'true'"
HOSTED = READY + " && runner.environment == 'github-hosted'"


def _setup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    cached: bool,
    ready: bool,
    recovery: bool = True,
    stall_once: bool = False,
    attempt_seconds: int | None = None,
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    executable = tmp_path / "python"
    calls = tmp_path / "calls"
    marker = tmp_path / "ready"
    stall = tmp_path / "stall"
    if ready:
        marker.touch()
    if stall_once:
        stall.touch()
    launch_stall = tmp_path / "launch_stall"
    if os.environ.get("BROWSER_CACHE_TEST_LAUNCH") == "stall":
        launch_stall.touch()
    executable.write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\n"
        'if [[ "$*" == *"install --with-deps chromium"* ]]; then\n'
        '  echo install >> "$CALLS"\n'
        '  [[ ! -e "$APT_LOCK" ]] || { echo "E: Could not get lock $APT_LOCK" >&2; exit 100; }\n'
        '  if [[ -f "$STALL" ]]; then\n'
        '    rm "$STALL"\n'
        '    if [[ -n "${BROWSER_CACHE_TEST_APT_SECONDS:-}" ]]; then\n'
        '      touch "$APT_LOCK"\n'
        '      setsid bash -c \'sleep "$BROWSER_CACHE_TEST_APT_SECONDS"; echo released >> "$CALLS"; rm "$APT_LOCK"\''
        " < /dev/null > /dev/null 2>&1 &\n"
        "    fi\n"
        "    sleep 30\n"
        "  fi\n"
        '  [[ "$RECOVERY" == true ]] || exit 1\n'
        '  touch "$READY"\n'
        "else\n"
        '  echo launch >> "$CALLS"\n'
        '  [[ -f "$READY" ]] || exit 1\n'
        '  if [[ -f "$LAUNCH_STALL" ]]; then rm "$LAUNCH_STALL"; sleep 30; fi\n'
        '  [[ "$LAUNCH_FAILS" != true ]]\n'
        "fi\n"
    )
    executable.chmod(0o755)
    sudo = tmp_path / "sudo"
    sudo.write_text('#!/usr/bin/env bash\nset -euo pipefail\necho mirror >> "$CALLS"\n')
    sudo.chmod(0o755)
    pgrep = tmp_path / "pgrep"
    pgrep.write_text('#!/usr/bin/env bash\n[[ "$*" == "-f apt-get" && -e "$APT_LOCK" ]]\n')
    pgrep.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    monkeypatch.setenv("CALLS", str(calls))
    monkeypatch.setenv("READY", str(marker))
    monkeypatch.setenv("STALL", str(stall))
    monkeypatch.setenv("APT_LOCK", str(tmp_path / "apt_lock"))
    monkeypatch.setenv("RECOVERY", str(recovery).lower())
    monkeypatch.setenv("LAUNCH_STALL", str(launch_stall))
    monkeypatch.setenv("LAUNCH_FAILS", str(os.environ.get("BROWSER_CACHE_TEST_LAUNCH") == "fail").lower())
    monkeypatch.setenv("CACHE_HIT", str(cached).lower())
    monkeypatch.setenv("GITHUB_ACTION_PATH", str(ACTION))
    output = tmp_path / "output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    action = yaml.safe_load((ACTION / "action.yml").read_text())
    steps = action["runs"]["steps"]
    start = next(i for i, step in enumerate(steps) if step.get("id") == "launch")
    for step in steps[start:]:
        if "run" not in step:
            continue
        condition = step.get("if")
        assert condition in (None, READY, HOSTED)
        if condition and output.read_text().strip() == "ready=true":
            continue
        if condition == HOSTED and os.environ.get("BROWSER_CACHE_TEST_RUNNER") == "self-hosted":
            continue
        env = {**os.environ, **{k: str(v) for k, v in step.get("env", {}).items() if "${{" not in str(v)}}
        if attempt_seconds is not None:
            env["ATTEMPT_SECONDS"] = str(attempt_seconds)
        result = subprocess.run(
            ["bash", "-euo", "pipefail", "-c", step["run"]], capture_output=True, text=True, env=env
        )
        if result.returncode:
            break
    return result, calls.read_text().splitlines() if calls.exists() else []


def test_verified_cached_browser_skips_browser_and_os_install(tmp_path, monkeypatch):
    result, calls = _setup(tmp_path, monkeypatch, cached=True, ready=True)
    assert result.returncode == 0, result.stderr
    assert calls == ["launch"]


@pytest.mark.parametrize("cached", [False, True])
def test_missing_browser_or_os_dependencies_are_recovered(tmp_path, monkeypatch, cached):
    result, calls = _setup(tmp_path, monkeypatch, cached=cached, ready=False)
    assert result.returncode == 0, result.stderr
    assert calls == (["launch"] if cached else []) + ["mirror", "install", "launch"]


def test_self_hosted_runner_recovers_without_the_hosted_mirror_edit(tmp_path, monkeypatch):
    monkeypatch.setenv("BROWSER_CACHE_TEST_RUNNER", "self-hosted")
    result, calls = _setup(tmp_path, monkeypatch, cached=False, ready=False)
    assert result.returncode == 0, result.stderr
    assert calls == ["install", "launch"]


def test_failed_browser_recovery_is_red_after_exactly_two_attempts(tmp_path, monkeypatch):
    result, calls = _setup(tmp_path, monkeypatch, cached=True, ready=False, recovery=False)
    assert result.returncode != 0
    assert calls == ["launch", "mirror", "install", "install"]
    assert result.stdout.count("::warning title=Retried browser install::") == 1


@pytest.mark.wall_clock
def test_a_stalled_download_is_killed_at_its_attempt_timeout_and_retried_green(tmp_path, monkeypatch):
    started = time.monotonic()
    result, calls = _setup(tmp_path, monkeypatch, cached=False, ready=False, stall_once=True, attempt_seconds=5)
    assert result.returncode == 0, result.stderr
    assert calls == ["mirror", "install", "install", "launch"]
    assert time.monotonic() - started < 20


def test_a_retry_after_a_timeout_waits_for_the_left_over_apt_get_to_release_its_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BROWSER_CACHE_TEST_APT_SECONDS", "8")
    result, calls = _setup(tmp_path, monkeypatch, cached=False, ready=False, stall_once=True, attempt_seconds=6)
    assert result.returncode == 0, result.stderr
    assert calls == ["mirror", "install", "released", "install", "launch"]


@pytest.mark.wall_clock
def test_a_retry_never_installs_while_the_lock_is_held_and_ends_within_its_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    apt_seconds = 8
    monkeypatch.setenv("BROWSER_CACHE_TEST_APT_SECONDS", str(apt_seconds))
    started = time.monotonic()
    result, calls = _setup(tmp_path, monkeypatch, cached=False, ready=False, stall_once=True, attempt_seconds=2)
    assert result.returncode != 0
    assert calls == ["mirror", "install"]
    assert result.stdout.count("::warning title=Retried browser install::") == 1
    assert time.monotonic() - started < apt_seconds


def test_the_runner_pgrep_matches_apt_get_inside_a_command_line_argument() -> None:
    holder = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)", "apt-get update&& apt-get install -y libnss3"]
    )
    try:
        found = subprocess.run(["pgrep", "-f", "apt-get"], capture_output=True, text=True)
    finally:
        holder.kill()
        holder.wait()
    assert str(holder.pid) in found.stdout.split()


def test_a_stalled_installed_browser_launch_is_retried_green(tmp_path, monkeypatch):
    monkeypatch.setenv("BROWSER_CACHE_TEST_LAUNCH", "stall")
    result, calls = _setup(tmp_path, monkeypatch, cached=False, ready=False, attempt_seconds=5)
    assert result.returncode == 0, result.stderr
    assert calls == ["mirror", "install", "launch", "install", "launch"]


def test_failed_installed_browser_launch_is_red_after_two_attempts(tmp_path, monkeypatch):
    monkeypatch.setenv("BROWSER_CACHE_TEST_LAUNCH", "fail")
    result, calls = _setup(tmp_path, monkeypatch, cached=False, ready=False)
    assert result.returncode != 0
    assert calls == ["mirror", "install", "launch", "install", "launch"]
    assert result.stdout.count("::warning title=Retried browser install::") == 1


def _timeouts(run):
    return sum(
        int(kill or 0) + int(seconds) for kill, seconds in re.findall(r"timeout (?:--kill-after=(\d+)s )?(\d+)s", run)
    )


def _attempt_budget():
    steps = yaml.safe_load((ACTION / "action.yml").read_text())["runs"]["steps"]
    install = next(step for step in steps if "install --with-deps chromium" in step.get("run", ""))
    attempt = int(install["env"]["ATTEMPT_SECONDS"])
    kill = int(
        re.search(
            r"timeout --kill-after=(\d+)s \"\$\{ATTEMPT_SECONDS\}s\" (?:python -m playwright install|bash)",
            install["run"],
        ).group(1)
    )
    others = sum(_timeouts(step.get("run", "")) for step in steps if step is not install)
    version_and_restore_seconds = 15
    return attempt, version_and_restore_seconds + others + 2 * (attempt + kill)


def test_each_attempt_has_room_for_the_measured_anton_install():
    attempt, _ = _attempt_budget()
    anton_image_install_seconds_2026_10_12 = 73
    assert attempt >= 1.5 * anton_image_install_seconds_2026_10_12


def _browser_cache_callers():
    for path in sorted((ROOT / ".github/workflows").glob("*.yml")):
        workflow = yaml.safe_load(path.read_text())
        for name, job in workflow["jobs"].items():
            for index, step in enumerate(job.get("steps", [])):
                if step.get("uses", "").endswith("/.github/actions/browser-cache"):
                    yield path.name, name, job["steps"], index


def test_every_browser_cache_caller_gives_its_step_both_attempts_and_names_that_bound():
    _, budget = _attempt_budget()
    callers = list(_browser_cache_callers())
    assert len(callers) >= 9
    words = {6: "six", 7: "seven", 8: "eight"}
    reported = 0
    for workflow, job, steps, index in callers:
        minutes = steps[index]["timeout-minutes"]
        assert minutes * 60 >= budget, (workflow, job)
        for step in steps[index + 1 :]:
            if step.get("name") == "Report browser setup failure":
                assert f"{words[minutes]} minute timeout" in step["run"], (workflow, job)
                reported += 1
    assert reported == 4


def test_cache_uses_installed_playwright_version_and_absolute_browser_folder():
    action = yaml.safe_load((ACTION / "action.yml").read_text())
    cache = next(step for step in action["runs"]["steps"] if step.get("uses") == "actions/cache/restore@v4")
    assert cache["with"]["path"] == "${{ github.workspace }}/.playwright"
    assert cache["with"]["key"] == ("chromium-${{ runner.os }}-${{ runner.arch }}-${{ steps.version.outputs.version }}")
    assert "restore-keys" not in cache["with"]
    stamp = next(step for step in action["runs"]["steps"] if step.get("id") == "version")
    assert "importlib.metadata" in stamp["run"]
    assert 'version("playwright")' in stamp["run"]


def test_only_a_dev_push_saves_the_browser_after_installing_it():
    steps = yaml.safe_load((ACTION / "action.yml").read_text())["runs"]["steps"]
    uses = [step.get("uses") for step in steps if "uses" in step]
    assert uses == ["actions/cache/restore@v4", "actions/cache/save@v4"]
    restore = next(step for step in steps if step.get("uses") == "actions/cache/restore@v4")
    save = next(step for step in steps if step.get("uses") == "actions/cache/save@v4")
    assert steps.index(save) == len(steps) - 1
    assert save["with"] == {"path": restore["with"]["path"], "key": "${{ steps.cache.outputs.cache-primary-key }}"}
    assert save["if"] == (
        "steps.launch.outputs.ready != 'true' && steps.cache.outputs.cache-hit != 'true'"
        " && github.event_name == 'push' && github.ref == 'refs/heads/dev'"
    )


@pytest.mark.parametrize(
    ("changed", "expected"),
    [
        ("hooks/plain.py", "false"),
        ("scripts/swarm_ledger/artifact_sanity.py", "true"),
        ("scripts/swarm_ledger/static/js/markdown.js", "true"),
        ("scripts/swarm_ledger/template.html", "true"),
        ("scripts/swarm_ledger/palette.css", "true"),
        ("tests/fixtures/artifacts/probe.md", "true"),
        ("pyproject.toml", "true"),
    ],
)
def test_lint_selects_browser_work_from_changed_artifact_inputs(tmp_path, monkeypatch, changed, expected):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GIT_AUTHOR_NAME", "CI test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "ci@example.test")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "CI test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "ci@example.test")
    subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "Base"], check=True)
    base = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    path = tmp_path / changed
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("probe\n")
    subprocess.run(["git", "add", changed], check=True)
    subprocess.run(["git", "commit", "-q", "-m", "Head"], check=True)
    output = tmp_path / "output"
    monkeypatch.setenv("BASE", base)
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    result = subprocess.run(["bash", str(ACTION / "select-artifacts.sh")], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert output.read_text() == f"browser={expected}\n"
