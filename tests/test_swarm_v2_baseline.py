import json
import shutil
import subprocess
from pathlib import Path

import pytest

import scripts.swarm_v2.baseline as baseline

FIXTURE = Path(__file__).parent / "fixtures" / "swarm_v2" / "baseline" / "sources.json"
REPOS = ("agentihooks", "agentibrain-kernel", "antoncore")
FIRST = "2026-10-07T00:00:00Z"
SECOND = "2026-10-08T00:00:00Z"


def _git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def _commit(work, message):
    (work / "file.txt").write_text(message)
    _git("add", "file.txt", cwd=work)
    _git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", message, cwd=work)
    _git("push", "-q", "origin", "HEAD:dev", cwd=work)
    return _git("rev-parse", "HEAD", cwd=work)


def _plant(root):
    shutil.copy(FIXTURE, root / "sources.json")
    heads = {}
    for name in REPOS:
        bare = root / f"{name}.git"
        _git("init", "-q", "--bare", str(bare), cwd=root)
        work = root / f"{name}-work"
        _git("clone", "-q", str(bare), str(work), cwd=root)
        _git("checkout", "-q", "-b", "dev", cwd=work)
        heads[name] = _commit(work, f"{name} first")
    return root / "sources.json", heads


def _refs(root):
    return {name: _git("for-each-ref", "--format=%(refname) %(objectname)", cwd=root / f"{name}.git") for name in REPOS}


def _collect(sources, now, previous=None):
    return baseline.collect(baseline.load_sources(sources), previous=previous, now=lambda: now)


def _by_repo(report):
    return {entry["repo"]: entry for entry in report["repositories"]}


@pytest.fixture
def planted(tmp_path):
    return _plant(tmp_path)


def test_report_separates_source_deployed_and_unknown_for_every_repo(planted):
    sources, heads = planted
    report = _collect(sources, FIRST)
    repos = _by_repo(report)
    assert sorted(repos) == sorted(REPOS)
    for name in REPOS:
        assert repos[name]["source"] == {"branch": "dev", "status": "resolved", "commit": heads[name]}
    assert repos["agentihooks"]["deployed"] == []
    assert repos["agentihooks"]["unknown_live"] == ["swarm controller version on Anton"]
    probe = repos["antoncore"]["deployed"][0]
    assert probe["name"] == "argocd sync revision"
    assert probe["status"] == "verified"
    assert probe["value"].startswith("git version ")
    assert repos["antoncore"]["unknown_live"] == ["live autoscaling group desired capacity"]
    assert report["observed_at"] == FIRST
    assert report["measurements"] == {"baseline_unverified_items": 2, "baseline_drift_items": 0}
    assert report["drift"] == []


def test_second_independent_fixture_gives_the_same_shape(tmp_path):
    first_root = tmp_path / "one"
    second_root = tmp_path / "two"
    first_root.mkdir()
    second_root.mkdir()
    first_sources, first_heads = _plant(first_root)
    second_sources, second_heads = _plant(second_root)
    first = _by_repo(_collect(first_sources, FIRST))
    second = _by_repo(_collect(second_sources, FIRST))
    for name in REPOS:
        assert first[name]["source"]["commit"] == first_heads[name]
        assert second[name]["source"]["commit"] == second_heads[name]
        assert first[name]["unknown_live"] == second[name]["unknown_live"]
        assert [p["status"] for p in first[name]["deployed"]] == [p["status"] for p in second[name]["deployed"]]


def test_unavailable_endpoint_is_unverified_never_empty(planted):
    sources, _ = planted
    root = sources.parent
    before = _refs(root)
    report = _collect(sources, FIRST)
    probe = _by_repo(report)["agentibrain-kernel"]["deployed"][0]
    assert probe["name"] == "brain-api version"
    assert probe["status"] == "unverified"
    assert probe["value"] is None
    assert probe["reason"]
    assert "unavailable.invalid" not in json.dumps(report)
    assert "brain-api version" in _by_repo(report)["agentibrain-kernel"]["unknown_live"]
    assert report["measurements"]["baseline_unverified_items"] == 2
    assert _refs(root) == before


def test_missing_command_and_missing_repo_are_unverified(planted):
    sources, _ = planted
    data = json.loads(sources.read_text())
    data["repositories"][2]["probes"] = [{"name": "cluster", "kind": "command", "argv": ["kubectl-absent-for-test"]}]
    data["repositories"][0]["url"] = "missing.git"
    sources.write_text(json.dumps(data))
    repos = _by_repo(_collect(sources, FIRST))
    assert repos["antoncore"]["deployed"][0] == {
        "name": "cluster",
        "status": "unverified",
        "value": None,
        "reason": "command not found",
    }
    assert repos["agentihooks"]["source"]["status"] == "unresolved"
    assert repos["agentihooks"]["source"]["commit"] is None
    assert "cluster" in repos["antoncore"]["unknown_live"]


def test_rerun_after_one_repo_advances_adds_drift_without_moving_branches(planted):
    sources, heads = planted
    root = sources.parent
    first = _collect(sources, FIRST)
    advanced = _commit(root / "antoncore-work", "antoncore second")
    before = _refs(root)
    second = _collect(sources, SECOND, previous=first)
    assert second["drift"] == [{"repo": "antoncore", "from": heads["antoncore"], "to": advanced, "observed_at": SECOND}]
    assert second["measurements"]["baseline_drift_items"] == 1
    assert _by_repo(second)["antoncore"]["source"]["commit"] == advanced
    assert _refs(root) == before
    third = _collect(sources, "2026-10-09T00:00:00Z", previous=second)
    assert third["drift"] == second["drift"]
    assert third["measurements"]["baseline_drift_items"] == 0


def test_unresolved_rerun_keeps_previous_commit_out_of_drift(planted):
    sources, _ = planted
    first = _collect(sources, FIRST)
    shutil.rmtree(sources.parent / "agentihooks.git")
    second = _collect(sources, SECOND, previous=first)
    assert second["drift"] == []
    assert _by_repo(second)["agentihooks"]["source"]["status"] == "unresolved"


@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        ("connect to http://10.0.0.5:8080/health failed", "connect to <redacted-url> failed"),
        ("host 192.168.1.20 refused", "host <redacted-ip> refused"),
        ("Authorization: Bearer abc.def", "Authorization: Bearer <redacted>"),
        ("token=ghp_secretvalue rest", "token=<redacted> rest"),
        ("plain text", "plain text"),
    ],
)
def test_sanitize_redacts_addresses_and_credentials(raw, clean):
    assert baseline.sanitize(raw) == clean


def test_markdown_lists_every_repo_and_unverified_state(planted):
    sources, heads = planted
    text = baseline.render_markdown(_collect(sources, FIRST))
    for name in REPOS:
        assert f"| {name} | dev `{heads[name]}` |" in text
    assert "brain-api version: unverified" in text
    assert "baseline_unverified_items: 2" in text
    assert "- agentihooks `gone.py`: missing" in text
    assert "- agentibrain-kernel `file.txt`: unverified (source head not in local checkout)" in text


def test_interfaces_are_present_missing_or_unverified(planted):
    sources, _ = planted
    repos = _by_repo(_collect(sources, FIRST))
    assert repos["agentihooks"]["interfaces"] == [
        {"path": "file.txt", "status": "present"},
        {"path": "gone.py", "status": "missing"},
    ]
    assert repos["agentibrain-kernel"]["interfaces"] == [
        {"path": "file.txt", "status": "unverified", "reason": "source head not in local checkout"}
    ]
    assert repos["antoncore"]["interfaces"] == []


@pytest.mark.parametrize(
    ("change", "reason"),
    [({"checkout": ""}, "no local checkout configured"), ({"url": "missing.git"}, "source head unresolved")],
)
def test_interfaces_without_checkout_or_head_are_unverified(planted, change, reason):
    sources, _ = planted
    data = json.loads(sources.read_text())
    data["repositories"][0].update(change)
    sources.write_text(json.dumps(data))
    interfaces = _by_repo(_collect(sources, FIRST))["agentihooks"]["interfaces"]
    assert interfaces == [
        {"path": "file.txt", "status": "unverified", "reason": reason},
        {"path": "gone.py", "status": "unverified", "reason": reason},
    ]


def test_cli_writes_json_and_markdown_and_reads_previous(planted, tmp_path):
    sources, _ = planted
    out_json = tmp_path / "out" / "baseline.json"
    out_md = tmp_path / "out" / "baseline.md"
    argv = ["--sources", str(sources), "--json", str(out_json), "--markdown", str(out_md)]
    assert baseline.main(argv) == 0
    first = json.loads(out_json.read_text())
    _commit(sources.parent / "agentihooks-work", "agentihooks second")
    assert baseline.main([*argv, "--previous", str(out_json)]) == 0
    second = json.loads(out_json.read_text())
    assert [d["repo"] for d in second["drift"]] == ["agentihooks"]
    assert second["observed_at"] >= first["observed_at"]
    assert out_md.read_text().startswith("# Swarm v2 implementation baseline")
