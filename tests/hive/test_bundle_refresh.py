import subprocess

import pytest

from scripts.hive import bundle_refresh

pytestmark = pytest.mark.unit
GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@example.com"]
ENSURE = (
    ["agentihooks", "deps", "ensure", "--quiet"],
    {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "start_new_session": True,
    },
)


def _commit(repo, name, text):
    (repo / name).write_text(text)
    subprocess.run([*GIT, "-C", str(repo), "add", name], check=True, capture_output=True)
    subprocess.run([*GIT, "-C", str(repo), "commit", "-m", name], check=True, capture_output=True)
    return _head(repo)


def _head(repo):
    return subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()


def _hive(tmp_path, root, branch="dev", first="deps.json"):
    remote = tmp_path / "remote"
    subprocess.run(["git", "init", f"--initial-branch={branch}", str(remote)], check=True, capture_output=True)
    _commit(remote, first, "raw gh")
    root.mkdir(parents=True)
    subprocess.run(
        ["git", "clone", "--branch", branch, str(remote), str(root / "agentihooks-bundle")],
        check=True,
        capture_output=True,
    )
    return remote, root / "agentihooks-bundle", {"HIVE_BUNDLE_URL": str(remote), "HIVE_REPO_ROOT": str(root)}


@pytest.fixture
def ensures(monkeypatch):
    calls, real = [], subprocess.Popen

    def popen(argv, *args, **kwargs):
        if argv[:1] != ["agentihooks"]:
            return real(argv, *args, **kwargs)
        calls.append((argv, kwargs))

    monkeypatch.setattr(bundle_refresh.subprocess, "Popen", popen)
    return calls


@pytest.mark.parametrize(("branch", "ref"), [("dev", ""), ("release", "release")])
def test_a_bundle_manifest_merged_after_the_pod_started_reaches_the_hive_and_reruns_ensure(
    tmp_path, ensures, branch, ref
):
    remote, bundle, environ = _hive(tmp_path, tmp_path / "root", branch)
    merged = _commit(remote, "deps.json", "wrapped gh")

    assert bundle_refresh.refresh(100.0, 700.0, {**environ, "HIVE_BUNDLE_REF": ref}) == 700.0
    assert _head(bundle) == merged
    assert ensures == [ENSURE]


def test_a_first_manifest_on_the_bundle_reruns_ensure(tmp_path, ensures):
    remote, bundle, environ = _hive(tmp_path, tmp_path / "root", first="README.md")
    _commit(remote, "deps.json", "wrapped gh")

    assert bundle_refresh.refresh(0.0, 900.0, environ) == 900.0
    assert (bundle / "deps.json").read_text() == "wrapped gh"
    assert ensures == [ENSURE]


def test_a_bundle_change_outside_the_manifest_moves_the_checkout_without_ensure(tmp_path, ensures):
    remote, bundle, environ = _hive(tmp_path, tmp_path / "root")
    merged = _commit(remote, "rules.md", "rule")

    assert bundle_refresh.refresh(0.0, 900.0, environ) == 900.0
    assert _head(bundle) == merged
    assert ensures == []


def test_the_bundle_is_left_alone_inside_the_refresh_window(tmp_path, ensures):
    remote, bundle, environ = _hive(tmp_path, tmp_path / "root")
    before = _head(bundle)
    _commit(remote, "deps.json", "wrapped gh")

    assert bundle_refresh.refresh(100.0, 699.0, environ) == 100.0
    assert _head(bundle) == before
    assert ensures == []


def test_a_hive_without_a_repo_root_uses_the_start_script_default(tmp_path, monkeypatch, ensures):
    home = tmp_path / "home"
    remote, bundle, environ = _hive(tmp_path, home / "dev")
    merged = _commit(remote, "deps.json", "wrapped gh")
    monkeypatch.setenv("HOME", str(home))

    assert bundle_refresh.refresh(0.0, 900.0, {**environ, "HIVE_REPO_ROOT": ""}) == 900.0
    assert _head(bundle) == merged


def test_a_hive_without_a_bundle_url_or_checkout_never_runs_git(tmp_path, monkeypatch):
    _, _, environ = _hive(tmp_path, tmp_path / "root")
    monkeypatch.setattr(bundle_refresh.subprocess, "run", lambda *a, **k: pytest.fail("git ran"))

    assert bundle_refresh.refresh(0.0, 900.0, {"HIVE_REPO_ROOT": environ["HIVE_REPO_ROOT"]}) == 0.0
    assert bundle_refresh.refresh(0.0, 900.0, {**environ, "HIVE_BUNDLE_URL": ""}) == 0.0
    assert bundle_refresh.refresh(0.0, 900.0, {**environ, "HIVE_REPO_ROOT": str(tmp_path / "empty")}) == 0.0


def test_a_failed_pull_keeps_the_checkout_and_waits_a_full_window(tmp_path, capfd, ensures):
    _, bundle, environ = _hive(tmp_path, tmp_path / "root")
    before = _head(bundle)

    assert bundle_refresh.refresh(0.0, 900.0, {**environ, "HIVE_BUNDLE_URL": str(tmp_path / "gone")}) == 900.0
    assert _head(bundle) == before
    assert ensures == []
    assert capfd.readouterr() == ("", "")


@pytest.mark.parametrize("error", [subprocess.TimeoutExpired("git", 60), OSError("no git")])
def test_a_stuck_or_missing_git_never_stops_the_daemon(tmp_path, monkeypatch, ensures, error):
    _, bundle, environ = _hive(tmp_path, tmp_path / "root")
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        raise error

    monkeypatch.setattr(bundle_refresh.subprocess, "run", run)

    assert bundle_refresh.refresh(0.0, 900.0, environ) == 900.0
    assert ensures == []
    assert calls == [
        (
            ["git", "-C", str(bundle), "pull", "--ff-only", "--", environ["HIVE_BUNDLE_URL"], "dev"],
            {"stdin": subprocess.DEVNULL, "capture_output": True, "timeout": 60},
        )
    ]


def test_a_missing_agentihooks_command_never_stops_the_daemon(tmp_path, monkeypatch):
    remote, bundle, environ = _hive(tmp_path, tmp_path / "root")
    merged = _commit(remote, "deps.json", "wrapped gh")
    real = subprocess.Popen

    def popen(argv, *args, **kwargs):
        if argv[:1] == ["agentihooks"]:
            raise FileNotFoundError("agentihooks")
        return real(argv, *args, **kwargs)

    monkeypatch.setattr(bundle_refresh.subprocess, "Popen", popen)

    assert bundle_refresh.refresh(0.0, 900.0, environ) == 900.0
    assert _head(bundle) == merged
