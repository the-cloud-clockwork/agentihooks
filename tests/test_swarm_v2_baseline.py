import json
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

import scripts.swarm_v2.baseline as baseline

FIXTURE = Path(__file__).parent / "fixtures" / "swarm_v2" / "baseline" / "sources.json"
REPOS = ("agentihooks", "agentibrain-kernel", "antoncore")
FIRST = "2026-10-07T00:00:00Z"
SECOND = "2026-10-08T00:00:00Z"
PY = sys.executable


def _git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def _commit(work, message, branch="dev"):
    (work / "file.txt").write_text(message)
    _git("add", "file.txt", cwd=work)
    _git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", message, cwd=work)
    _git("push", "-q", "origin", f"HEAD:{branch}", cwd=work)
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


def _command(*argv, name="probe", **extra):
    return baseline.Probe(name=name, kind="command", argv=(*argv, "--version"), **extra)


@pytest.fixture
def planted(tmp_path):
    return _plant(tmp_path)


@pytest.fixture
def server():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = {"/ok": b'{"version": "1.2"}', "/bare": b'{"status": "ok"}', "/list": b"[1]"}.get(
                self.path, b"<html>"
            )
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()
    httpd.server_close()


def test_report_separates_source_deployed_and_unknown_for_every_repo(planted):
    sources, heads = planted
    report = _collect(sources, FIRST)
    repos = _by_repo(report)
    assert [r["repo"] for r in report["repositories"]] == list(REPOS)
    for name in REPOS:
        assert repos[name]["source"] == {"branch": "dev", "status": "resolved", "commit": heads[name]}
    assert repos["agentihooks"]["deployed"] == []
    assert repos["agentihooks"]["unknown_live"] == ["swarm controller version on Anton"]
    git_version = subprocess.run(["git", "--version"], capture_output=True, text=True, check=True).stdout.strip()
    assert repos["antoncore"]["deployed"] == [
        {"name": "argocd sync revision", "status": "verified", "value": git_version}
    ]
    assert repos["antoncore"]["unknown_live"] == ["live autoscaling group desired capacity"]
    assert report["schema"] == "swarm-v2-baseline/1"
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
        assert first[name]["deployed"] == second[name]["deployed"]
        assert first[name]["interfaces"] == second[name]["interfaces"]


def test_unavailable_endpoint_is_unverified_never_empty(planted):
    sources, _ = planted
    root = sources.parent
    before = _refs(root)
    report = _collect(sources, FIRST)
    brain = _by_repo(report)["agentibrain-kernel"]
    assert brain["deployed"] == [
        {"name": "brain-api version", "status": "unverified", "value": None, "reason": "unreachable (URLError)"}
    ]
    assert brain["unknown_live"] == ["brain-api version"]
    assert "unavailable.invalid" not in json.dumps(report)
    assert report["measurements"]["baseline_unverified_items"] == 2
    assert _refs(root) == before


def test_missing_command_and_missing_repo_are_unverified(planted):
    sources, _ = planted
    data = json.loads(sources.read_text())
    data["repositories"][2]["probes"] = [
        {"name": "cluster", "kind": "command", "argv": ["kubectl-absent", "--version"]}
    ]
    data["repositories"][0]["url"] = "missing.git"
    sources.write_text(json.dumps(data))
    repos = _by_repo(_collect(sources, FIRST))
    assert repos["antoncore"]["deployed"] == [
        {"name": "cluster", "status": "unverified", "value": None, "reason": "command not found"}
    ]
    source = repos["agentihooks"]["source"]
    assert {k: source[k] for k in ("branch", "status", "commit")} == {
        "branch": "dev",
        "status": "unresolved",
        "commit": None,
    }
    assert source["reason"].startswith("exit 128: fatal: ")
    assert repos["antoncore"]["unknown_live"] == ["live autoscaling group desired capacity", "cluster"]


def test_branch_absent_from_remote_is_unresolved(planted):
    sources, _ = planted
    assert baseline.resolve_head(str(sources.parent / "antoncore.git"), "main") == {
        "branch": "main",
        "status": "unresolved",
        "commit": None,
        "reason": "refs/heads/main not found",
    }


def test_git_and_command_timeouts_are_unresolved(monkeypatch):
    def hang(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr(baseline.subprocess, "run", hang)
    assert baseline.resolve_head("x.git", "dev") == {
        "branch": "dev",
        "status": "unresolved",
        "commit": None,
        "reason": "timed out",
    }
    assert baseline.observe(_command("x"), {"commit": None}) == {
        "name": "probe",
        "status": "unverified",
        "value": None,
        "reason": "timed out",
    }


@pytest.mark.parametrize(
    ("code", "reason"),
    [
        ("import sys; sys.stderr.write('bad http://10.0.0.1/x '); sys.exit(3)", "exit 3: bad <redacted-url>"),
        ("", "empty answer"),
    ],
)
def test_failing_or_empty_command_is_unverified(code, reason):
    assert baseline.observe(_command(PY, "-c", code), {"commit": None}) == {
        "name": "probe",
        "status": "unverified",
        "value": None,
        "reason": reason,
    }


def test_command_output_is_stripped_and_sanitized():
    probe = _command(PY, "-c", "print('  host 10.1.2.3  ')")
    assert baseline.observe(probe, {"commit": None}) == {
        "name": "probe",
        "status": "verified",
        "value": "host <redacted-ip>",
    }


@pytest.mark.parametrize(("output", "value", "matches"), [("abc abc", "abc", True), ("def abc", "abc def", False)])
def test_unique_revision_probe_compares_with_source(output, value, matches):
    probe = _command(PY, "-c", f"print('{output}')", unique=True, revision=True)
    assert baseline.observe(probe, {"commit": "abc"}) == {
        "name": "probe",
        "status": "verified",
        "value": value,
        "matches_source": matches,
    }
    assert baseline.observe(probe, {"commit": None}) == {"name": "probe", "status": "verified", "value": value}


def test_unsupported_probe_kind_is_unverified():
    probe = baseline.Probe(name="p", kind="ftp")
    assert baseline.observe(probe, {"commit": None}) == {
        "name": "p",
        "status": "unverified",
        "value": None,
        "reason": "unsupported probe kind ftp",
    }


@pytest.mark.parametrize(
    ("path", "status", "value", "reason"),
    [
        ("/ok", "verified", "1.2", None),
        ("/bare", "unverified", None, "endpoint answered without version"),
        ("/list", "unverified", None, "endpoint answered without version"),
        ("/html", "unverified", None, "answer is not JSON"),
    ],
)
def test_http_probe(server, monkeypatch, path, status, value, reason):
    monkeypatch.setenv("BASELINE_TEST_URL", server)
    probe = baseline.Probe(name="h", kind="http", url_env="BASELINE_TEST_URL", path=path, field="version")
    expected = {"name": "h", "status": status, "value": value}
    if reason:
        expected["reason"] = reason
    assert baseline.observe(probe, {"commit": None}) == expected
    direct = baseline.Probe(name="h", kind="http", url=server, path=path, field="version")
    assert baseline.observe(direct, {"commit": None}) == expected


def test_http_probe_without_configured_url(monkeypatch):
    monkeypatch.delenv("BASELINE_TEST_URL", raising=False)
    probe = baseline.Probe(name="h", kind="http", url_env="BASELINE_TEST_URL", field="version")
    assert baseline.observe(probe, {"commit": None}) == {
        "name": "h",
        "status": "unverified",
        "value": None,
        "reason": "BASELINE_TEST_URL is not set",
    }


@pytest.mark.parametrize(
    ("argv", "allowed"),
    [
        (("agentihooks", "--version"), True),
        (("kubectl", "--context", "c", "get", "pods"), True),
        (("kubectl", "describe", "node"), True),
        (("kubectl", "version"), True),
        (("kubectl", "delete", "pod", "get"), False),
        (("kubectl", "rollout", "restart", "get"), False),
        (("kubectl", "logs", "x"), False),
        (("git", "push"), False),
        (("bash", "-c", "kubectl get pods"), False),
        ((), False),
    ],
)
def test_read_only_allows_only_reads(argv, allowed):
    assert baseline.read_only(argv) is allowed


def test_load_sources_refuses_a_writing_probe(planted):
    sources, _ = planted
    data = json.loads(sources.read_text())
    data["repositories"][2]["probes"] = [{"name": "bad", "kind": "command", "argv": ["kubectl", "delete", "pod", "x"]}]
    sources.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="^probe bad is not a read-only command$"):
        baseline.load_sources(sources)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("git@github.com:o/r.git", "git@github.com:o/r.git"),
        ("https://github.com/o/r.git", "https://github.com/o/r.git"),
        ("/abs/r.git", "/abs/r.git"),
        ("rel.git", "/base/rel.git"),
        ("", ""),
    ],
)
def test_repo_url_forms(url, expected):
    assert baseline._repo_url(url, Path("/base")) == expected


def test_repo_url_expands_home(monkeypatch):
    monkeypatch.setenv("HOME", "/home/someone")
    assert baseline._repo_url("~/dev/r", Path("/base")) == "/home/someone/dev/r"


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


@pytest.mark.parametrize("key", ["token", "TOKEN", "password", "secret", "api_key", "apikey"])
def test_sanitize_redacts_credential_assignments(key):
    assert baseline.sanitize(f"{key}=v1&x=2 rest") == f"{key}=<redacted>&x=2 rest"


@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        ("connect to http://10.0.0.5:8080/health failed", "connect to <redacted-url> failed"),
        ("host 192.168.1.20 refused", "host <redacted-ip> refused"),
        ("Authorization: Bearer abc.def", "Authorization: Bearer <redacted>"),
        ("version 1.2.3 ok", "version 1.2.3 ok"),
        ("plain text", "plain text"),
    ],
)
def test_sanitize_redacts_addresses_and_bearers(raw, clean):
    assert baseline.sanitize(raw) == clean


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


def test_markdown_renders_every_state_exactly():
    report = {
        "observed_at": FIRST,
        "repositories": [
            {
                "repo": "a",
                "source": {"branch": "dev", "status": "resolved", "commit": "c1"},
                "interfaces": [{"path": "x.py", "status": "present"}],
                "deployed": [
                    {"name": "rev", "status": "verified", "value": "c1", "matches_source": True},
                    {"name": "old", "status": "verified", "value": "c0", "matches_source": False},
                    {"name": "img", "status": "verified", "value": "i:1"},
                    {"name": "api", "status": "unverified", "value": None, "reason": "a|b\nc"},
                ],
                "unknown_live": ["api"],
            },
            {
                "repo": "b",
                "source": {"branch": "dev", "status": "unresolved", "commit": None, "reason": "timed out"},
                "interfaces": [{"path": "y.py", "status": "unverified", "reason": "source head unresolved"}],
                "deployed": [],
                "unknown_live": [],
            },
        ],
        "drift": [{"repo": "a", "from": "c0", "to": "c1", "observed_at": SECOND}],
        "measurements": {"baseline_unverified_items": 2, "baseline_drift_items": 1},
    }
    assert baseline.render_markdown(report) == (
        "# Swarm v2 implementation baseline\n"
        "\n"
        "Package SV2-FND-01. Observed at 2026-10-07T00:00:00Z.\n"
        "Source is the branch head. Deployed values come from read-only probes.\n"
        "An unverified value is unknown: it never means empty, absent or zero.\n"
        f"Regenerate with `{baseline.REGENERATE}`.\n"
        "\n"
        "| Repository | Source | Deployed | Unknown live values |\n"
        "|---|---|---|---|\n"
        "| a | dev `c1` | rev: verified `c1`, matches source; old: verified `c0`, differs from source; "
        "img: verified `i:1`; api: unverified (a\\|b c) | api |\n"
        "| b | dev unresolved (timed out) | none probed | none |\n"
        "\n"
        "## Source-proven interfaces\n"
        "\n"
        "- a `x.py`: present\n"
        "- b `y.py`: unverified (source head unresolved)\n"
        "\n"
        "## Drift\n"
        "\n"
        "- 2026-10-08T00:00:00Z a: `c0` to `c1`\n"
        "\n"
        "## Measurements\n"
        "\n"
        "- baseline_unverified_items: 2\n"
        "- baseline_drift_items: 1\n"
    )


def test_markdown_for_an_empty_report():
    report = {"observed_at": FIRST, "repositories": [], "drift": [], "measurements": {}}
    assert baseline.render_markdown(report).endswith(
        "|---|---|---|---|\n\n## Source-proven interfaces\n\nNone configured.\n\n## Drift\n\nNone recorded.\n\n"
        "## Measurements\n\n"
    )


def test_markdown_from_planted_fixture(planted):
    sources, heads = planted
    text = baseline.render_markdown(_collect(sources, FIRST))
    assert f"| agentihooks | dev `{heads['agentihooks']}` | none probed | swarm controller version on Anton |\n" in text
    assert (
        f"| agentibrain-kernel | dev `{heads['agentibrain-kernel']}` | "
        "brain-api version: unverified (unreachable (URLError)) | brain-api version |\n"
    ) in text


def test_utc_now_format():
    stamp = baseline._utc_now()
    assert len(stamp) == 20
    assert stamp[4] + stamp[7] + stamp[10] + stamp[13] + stamp[16] + stamp[19] == "--T::Z"


def test_cli_writes_json_and_markdown_and_reads_previous(planted, tmp_path, monkeypatch):
    sources, _ = planted
    out_json = tmp_path / "out" / "baseline.json"
    out_md = tmp_path / "out" / "baseline.md"
    argv = ["--sources", str(sources), "--json", str(out_json), "--markdown", str(out_md)]
    monkeypatch.setattr(baseline, "_utc_now", lambda: FIRST)
    assert baseline.main(argv) == 0
    first = json.loads(out_json.read_text())
    assert first["observed_at"] == FIRST
    assert out_json.read_text() == json.dumps(first, indent=2) + "\n"
    assert out_md.read_text() == baseline.render_markdown(first)
    assert sorted(p.name for p in out_json.parent.iterdir()) == ["baseline.json", "baseline.md"]
    advanced = _commit(sources.parent / "agentihooks-work", "agentihooks second")
    monkeypatch.setattr(baseline, "_utc_now", lambda: SECOND)
    assert baseline.main([*argv, "--previous", str(out_json)]) == 0
    second = json.loads(out_json.read_text())
    assert second["observed_at"] == SECOND
    assert second["drift"] == [
        {
            "repo": "agentihooks",
            "from": first["repositories"][0]["source"]["commit"],
            "to": advanced,
            "observed_at": SECOND,
        }
    ]
