import subprocess

import pytest

from scripts.hive import bundle_refresh

pytestmark = pytest.mark.unit
GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@example.com"]


def _commit(repo, name):
    (repo / name).write_text(name)
    subprocess.run([*GIT, "-C", str(repo), "add", name], check=True, capture_output=True)
    subprocess.run([*GIT, "-C", str(repo), "commit", "-m", name], check=True, capture_output=True)
    return _head(repo)


def _head(repo):
    return subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()


def _hive(tmp_path, branch="dev"):
    remote = tmp_path / "remote"
    subprocess.run(["git", "init", f"--initial-branch={branch}", str(remote)], check=True, capture_output=True)
    _commit(remote, "deps.json")
    root = tmp_path / "root"
    root.mkdir()
    subprocess.run(
        ["git", "clone", "--branch", branch, str(remote), str(root / "agentihooks-bundle")],
        check=True,
        capture_output=True,
    )
    environ = {"HIVE_BUNDLE_URL": str(remote), "HIVE_REPO_ROOT": str(root)}
    if branch != "dev":
        environ["HIVE_BUNDLE_REF"] = branch
    return remote, root / "agentihooks-bundle", environ


@pytest.mark.parametrize("branch", ["dev", "release"])
def test_a_bundle_commit_merged_after_the_pod_started_reaches_the_hive_checkout(tmp_path, branch):
    remote, bundle, environ = _hive(tmp_path, branch)
    merged = _commit(remote, "wrapper")

    assert bundle_refresh.refresh(100.0, 700.0, environ) == 700.0
    assert _head(bundle) == merged


def test_the_bundle_is_left_alone_inside_the_refresh_window(tmp_path):
    remote, bundle, environ = _hive(tmp_path)
    before = _head(bundle)
    _commit(remote, "wrapper")

    assert bundle_refresh.refresh(100.0, 699.0, environ) == 100.0
    assert _head(bundle) == before


def test_a_hive_without_a_bundle_url_or_checkout_never_runs_git(tmp_path, monkeypatch):
    _, bundle, environ = _hive(tmp_path)
    monkeypatch.setattr(bundle_refresh.subprocess, "run", lambda *a, **k: pytest.fail("git ran"))

    assert bundle_refresh.refresh(0.0, 900.0, {"HIVE_REPO_ROOT": environ["HIVE_REPO_ROOT"]}) == 0.0
    assert bundle_refresh.refresh(0.0, 900.0, {**environ, "HIVE_BUNDLE_URL": ""}) == 0.0
    assert bundle_refresh.refresh(0.0, 900.0, {**environ, "HIVE_REPO_ROOT": str(tmp_path / "empty")}) == 0.0


def test_a_failed_pull_keeps_the_checkout_and_waits_a_full_window(tmp_path, capfd):
    _, bundle, environ = _hive(tmp_path)
    before = _head(bundle)

    assert bundle_refresh.refresh(0.0, 900.0, {**environ, "HIVE_BUNDLE_URL": str(tmp_path / "gone")}) == 900.0
    assert _head(bundle) == before
    assert capfd.readouterr() == ("", "")


@pytest.mark.parametrize("error", [subprocess.TimeoutExpired("git", 120), OSError("no git")])
def test_a_stuck_or_missing_git_never_stops_the_daemon(tmp_path, monkeypatch, error):
    _, bundle, environ = _hive(tmp_path)
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        raise error

    monkeypatch.setattr(bundle_refresh.subprocess, "run", run)

    assert bundle_refresh.refresh(0.0, 900.0, environ) == 900.0
    assert calls == [
        (
            ["git", "-C", str(bundle), "pull", "--ff-only", "--", environ["HIVE_BUNDLE_URL"], "dev"],
            {"capture_output": True, "timeout": 120},
        )
    ]
