import os
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
ACTION = ROOT / ".github/actions/browser-cache"


def _setup(tmp_path, monkeypatch, *, cached, ready, recovery=True):
    executable = tmp_path / "python"
    calls = tmp_path / "calls"
    marker = tmp_path / "ready"
    if ready:
        marker.touch()
    executable.write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\n"
        'if [[ "$*" == *"install --with-deps chromium"* ]]; then\n'
        '  echo install >> "$CALLS"\n'
        '  [[ "$RECOVERY" == true ]] || exit 1\n'
        '  touch "$READY"\n'
        "else\n"
        '  echo launch >> "$CALLS"\n'
        '  [[ -f "$READY" ]]\n'
        "fi\n"
    )
    executable.chmod(0o755)
    sudo = tmp_path / "sudo"
    sudo.write_text('#!/usr/bin/env bash\nset -euo pipefail\necho mirror >> "$CALLS"\n')
    sudo.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    monkeypatch.setenv("CALLS", str(calls))
    monkeypatch.setenv("READY", str(marker))
    monkeypatch.setenv("RECOVERY", str(recovery).lower())
    monkeypatch.setenv("CACHE_HIT", str(cached).lower())
    monkeypatch.setenv("GITHUB_ACTION_PATH", str(ACTION))
    output = tmp_path / "output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    action = yaml.safe_load((ACTION / "action.yml").read_text())
    steps = action["runs"]["steps"]
    start = next(i for i, step in enumerate(steps) if step.get("id") == "launch")
    for step in steps[start:]:
        condition = step.get("if")
        assert condition in (None, "steps.launch.outputs.ready != 'true'")
        if condition and output.read_text().strip() == "ready=true":
            continue
        result = subprocess.run(["bash", "-euo", "pipefail", "-c", step["run"]], capture_output=True, text=True)
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


def test_failed_browser_recovery_is_red(tmp_path, monkeypatch):
    result, calls = _setup(tmp_path, monkeypatch, cached=True, ready=False, recovery=False)
    assert result.returncode != 0
    assert calls == ["launch", "mirror", "install"]


def test_cache_uses_installed_playwright_version_and_absolute_browser_folder():
    action = yaml.safe_load((ACTION / "action.yml").read_text())
    cache = next(step for step in action["runs"]["steps"] if step.get("uses") == "actions/cache@v4")
    assert cache["with"]["path"] == "${{ github.workspace }}/.playwright"
    assert cache["with"]["key"] == ("chromium-${{ runner.os }}-${{ runner.arch }}-${{ steps.version.outputs.version }}")
    assert "restore-keys" not in cache["with"]
    stamp = next(step for step in action["runs"]["steps"] if step.get("id") == "version")
    assert "importlib.metadata" in stamp["run"]
    assert 'version("playwright")' in stamp["run"]


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
