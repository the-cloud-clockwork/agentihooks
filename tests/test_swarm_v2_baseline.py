import json
import shutil
import subprocess
import sys
import threading
from datetime import UTC, datetime
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


def _plant(root, tag=""):
    shutil.copy(FIXTURE, root / "sources.json")
    _git("init", "-q", "--bare", str(root / "stale-checkout.git"), cwd=root)
    heads = {}
    for name in REPOS:
        bare = root / f"{name}.git"
        _git("init", "-q", "--bare", str(bare), cwd=root)
        work = root / f"{name}-work"
        _git("clone", "-q", str(bare), str(work), cwd=root)
        _git("checkout", "-q", "-b", "dev", cwd=work)
        heads[name] = _commit(work, f"{name} first{tag}")
    return root / "sources.json", heads


def _refs(root):
    return {name: _git("for-each-ref", "--format=%(refname) %(objectname)", cwd=root / f"{name}.git") for name in REPOS}


def _collect(sources, now, previous=None):
    return baseline.collect(baseline.load_sources(sources), previous=previous, now=lambda: now)


def _by_repo(report):
    return {entry["repo"]: entry for entry in report["repositories"]}


def _command(*argv, name="probe", **extra):
    return baseline.Probe(name=name, kind="command", argv=argv, **extra)


@pytest.fixture
def planted(tmp_path):
    return _plant(tmp_path)


@pytest.fixture
def server():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = {
                "/ok": b'{"version": "1.2"}',
                "/bare": b'{"status": "ok"}',
                "/list": b"[1]",
                "/empty": b'{"version": ""}',
            }.get(self.path, b"<html>")
            self.send_response(500 if self.path == "/err" else 200)
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
        {"name": "read-only command stand-in", "status": "verified", "value": git_version, "matches_source": False}
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
    second_sources, second_heads = _plant(second_root, " again")
    assert all(first_heads[name] != second_heads[name] for name in REPOS)
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
    data["repositories"][0]["url"] = "missing.git"
    sources.write_text(json.dumps(data))
    repos = _by_repo(_collect(sources, FIRST))
    assert baseline.observe(_command("kubectl-absent", name="cluster"), {"commit": None}) == {
        "name": "cluster",
        "status": "unverified",
        "value": None,
        "reason": "command not found",
    }
    source = repos["agentihooks"]["source"]
    assert {k: source[k] for k in ("branch", "status", "commit")} == {
        "branch": "dev",
        "status": "unresolved",
        "commit": None,
    }
    assert source["reason"].startswith("exit 128: fatal: ")
    assert repos["agentihooks"]["unknown_live"] == ["swarm controller version on Anton"]


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
    assert baseline._git_ok("x", "status") is None
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
        ("/empty", "unverified", None, "empty answer"),
        ("/err", "unverified", None, "http 500"),
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
        (("git", "--version"), True),
        (("git", "--version", "x"), False),
        (("rm", "x", "--version"), False),
        (("kubectl", "delete", "pod", "x", "--version"), False),
        (("kubectl", "config", "use-context", "describe"), False),
        (("kubectl", "debug", "node/x", "--image=busybox", "get"), False),
        (("kubectl", "--context", "get", "delete"), False),
        (
            ("kubectl", "-n", "ns", "--namespace", "ns", "--kubeconfig", "k", "--cluster", "c", "--user", "u", "get"),
            True,
        ),
        (("kubectl", "--request-timeout=5s", "-o", "get"), False),
        (("kubectl", "--request-timeout=5s", "get", "pods"), True),
        (("kubectl", "--context"), False),
        (("kubectl",), False),
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
    ids=["url", "ip", "bearer", "version", "plain"],
)
def test_sanitize_redacts_addresses_and_bearers(raw, clean):
    assert baseline.sanitize(raw) == clean


@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        ("from git@github.com:o/r.git ok", "from <redacted-url> ok"),
        ("image ghcr.io/o/app@sha256:abc123 ok", "image ghcr.io/o/app@sha256:abc123 ok"),
        ("node 2001:db8::8a2e:370:7334 down", "node <redacted-ip> down"),
        ("loop ::1 and fe80::1", "loop <redacted-ip> and <redacted-ip>"),
        ("at 19:28:16 and a:b", "at 19:28:16 and a:b"),
        ("git@anton:repo.git ok", "<redacted-url> ok"),
        ("dial tcp anton.internal:6443 refused", "dial tcp <redacted-host> refused"),
        ("registry localhost:5000 up", "registry <redacted-host> up"),
        ("cluster-autoscaler:v1.33.0 exit 1: error", "cluster-autoscaler:v1.33.0 exit 1: error"),
        ("digest sha256:1234567 ok", "digest sha256:1234567 ok"),
    ],
)
def test_sanitize_redacts_ssh_and_ipv6_addresses(raw, clean):
    assert baseline.sanitize(raw) == clean


@pytest.mark.parametrize("key", ["token", "Password", "secret", "api_key"])
def test_sanitize_redacts_colon_credentials(key):
    assert baseline.sanitize(f"{key}: v1 rest") == f"{key}: <redacted> rest"


@pytest.mark.parametrize(
    "prefix", ["gh" + "p_", "gh" + "r_", "gh" + "s_", "github" + "_pat_", "gl" + "pat-", "s" + "k-", "xo" + "xb-"]
)
def test_sanitize_redacts_bare_tokens(prefix):
    assert baseline.sanitize(f"use {prefix}{'A1' * 6} now") == "use <redacted> now"
    assert baseline.sanitize(f"use {prefix}short now") == f"use {prefix}short now"


def test_sanitize_redacts_aws_access_key_ids():
    key = "AK" + "IA" + "Q" * 16
    assert baseline.sanitize(f"id {key} ok") == "id <redacted> ok"
    assert baseline.sanitize(f"id {key[:-1]} ok") == f"id {key[:-1]} ok"


def test_missing_git_binary(monkeypatch, planted):
    sources, _ = planted

    def absent(*args, **kwargs):
        raise FileNotFoundError(args[0][0])

    monkeypatch.setattr(baseline.subprocess, "run", absent)
    assert baseline.resolve_head("x.git", "dev") == {
        "branch": "dev",
        "status": "unresolved",
        "commit": None,
        "reason": "git not found",
    }
    assert baseline._git_ok("x", "status") is None


def test_git_that_does_not_answer_leaves_interfaces_unverified(planted, monkeypatch):
    sources, heads = planted
    repo = baseline.load_sources(sources).repositories[0]
    monkeypatch.setattr(baseline, "_git_ok", lambda checkout, *args: None)
    assert baseline.check_interfaces(repo, heads["agentihooks"]) == [
        {"path": "file.txt", "status": "unverified", "reason": "git did not answer"},
        {"path": "gone.py", "status": "unverified", "reason": "git did not answer"},
    ]
    monkeypatch.setattr(baseline, "_git_ok", lambda checkout, *args: True if args[-1].endswith("}") else None)
    assert baseline.check_interfaces(repo, heads["agentihooks"]) == [
        {"path": "file.txt", "status": "unverified", "reason": "git did not answer"},
        {"path": "gone.py", "status": "unverified", "reason": "git did not answer"},
    ]


def _cli(sources, tmp_path, previous=None):
    argv = ["--sources", str(sources), "--json", str(tmp_path / "b.json"), "--markdown", str(tmp_path / "b.md")]
    return baseline.main([*argv, "--previous", str(previous)] if previous else argv)


def test_interrupted_write_recovers_on_rerun(planted, tmp_path, monkeypatch):
    sources, _ = planted
    out = tmp_path / "out"
    monkeypatch.setattr(baseline, "_utc_now", lambda: FIRST)
    _cli(sources, out)
    _commit(sources.parent / "antoncore-work", "antoncore second")
    real_write = baseline._write

    def crash_on_markdown(path, text):
        if path.suffix == ".md":
            raise OSError("disk gone")
        real_write(path, text)

    monkeypatch.setattr(baseline, "_write", crash_on_markdown)
    monkeypatch.setattr(baseline, "_utc_now", lambda: SECOND)
    with pytest.raises(OSError, match="disk gone"):
        _cli(sources, out, out / "b.json")
    monkeypatch.setattr(baseline, "_write", real_write)
    monkeypatch.setattr(baseline, "_utc_now", lambda: "2026-10-09T00:00:00Z")
    assert _cli(sources, out, out / "b.json") == 0
    report = json.loads((out / "b.json").read_text())
    assert [d["repo"] for d in report["drift"]] == ["antoncore"]
    assert (out / "b.md").read_text() == baseline.render_markdown(report)
    assert sorted(p.name for p in out.iterdir()) == ["b.json", "b.md"]


@pytest.mark.parametrize("use_previous", [False, True])
def test_older_previous_cannot_overwrite_newer_output(planted, tmp_path, monkeypatch, use_previous, capsys):
    sources, _ = planted
    out = tmp_path / "out"
    older = tmp_path / "older.json"
    older.write_text(json.dumps({"observed_at": "2026-10-06", "repositories": [], "drift": []}))
    monkeypatch.setattr(baseline, "_utc_now", lambda: FIRST)
    _cli(sources, out)
    before = (out / "b.json").read_text()
    with pytest.raises(SystemExit) as exit_info:
        _cli(sources, out, older if use_previous else None)
    assert exit_info.value.code == 2
    assert f"{out / 'b.json'} holds a newer baseline; pass it as --previous" in capsys.readouterr().err
    assert (out / "b.json").read_text() == before


def _record_run(monkeypatch, stdout="", returncode=0):
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, returncode, stdout, "")

    monkeypatch.setattr(baseline.subprocess, "run", run)
    return calls


RUN_KWARGS = {"capture_output": True, "text": True, "check": False}


def test_subprocess_calls_capture_text_with_bounded_timeouts(monkeypatch):
    calls = _record_run(monkeypatch, stdout="v\n")
    baseline.resolve_head("u.git", "dev")
    baseline.observe(_command("tool"), {"commit": None})
    assert baseline._git_ok("c", "status") is True
    assert calls == [
        (["git", "ls-remote", "u.git", "refs/heads/dev"], {**RUN_KWARGS, "timeout": baseline.GIT_TIMEOUT}),
        (["tool"], {**RUN_KWARGS, "timeout": baseline.COMMAND_TIMEOUT}),
        (["git", "-C", "c", "status"], {**RUN_KWARGS, "timeout": baseline.GIT_TIMEOUT}),
    ]


def test_http_probe_uses_bounded_timeout(monkeypatch):
    seen = []

    def urlopen(url, timeout):
        seen.append((url, timeout))
        raise OSError("down")

    monkeypatch.setattr(baseline.urllib.request, "urlopen", urlopen)
    probe = baseline.Probe(name="h", kind="http", url="http://h", path="/p", field="v")
    assert baseline.observe(probe, {"commit": None})["reason"] == "unreachable (OSError)"
    assert seen == [("http://h/p", baseline.HTTP_TIMEOUT)]


def test_ref_line_with_extra_tab_is_not_the_branch(monkeypatch):
    _record_run(monkeypatch, stdout="abc\tjunk\trefs/heads/dev\n")
    assert baseline.resolve_head("u.git", "dev")["reason"] == "refs/heads/dev not found"


def test_unverified_count_weighs_each_item_once():
    verified = {"status": "verified"}
    repos = [
        {"source": {"status": "resolved"}, "deployed": [verified, verified], "interfaces": [{"status": "present"}]},
        {"source": {"status": "unresolved"}, "deployed": [], "interfaces": [{"status": "unverified"}]},
    ]
    assert baseline._unverified_count(repos) == 2


def test_minimal_repository_entry_loads_with_empty_defaults(tmp_path):
    sources = tmp_path / "s.json"
    sources.write_text(json.dumps({"branch": "dev", "repositories": [{"name": "r", "url": "r.git"}]}))
    assert baseline.load_sources(str(sources)) == baseline.Sources(
        branch="dev",
        repositories=(baseline.Repository(name="r", url=str(tmp_path / "r.git"), probes=(), unknown_live=()),),
    )


def test_cli_requires_every_path_and_describes_itself(capsys):
    with pytest.raises(SystemExit):
        baseline.main([])
    assert "the following arguments are required: --sources, --json, --markdown" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        baseline.main(["--help"])
    assert "\n\nRecord Swarm v2 source and deployment baselines read-only.\n\n" in capsys.readouterr().out


def test_missing_previous_file_is_refused(planted, tmp_path, capsys):
    sources, _ = planted
    with pytest.raises(SystemExit) as exit_info:
        _cli(sources, tmp_path / "out", tmp_path / "absent.json")
    assert exit_info.value.code == 2
    assert f"{tmp_path / 'absent.json'} does not exist" in capsys.readouterr().err
    assert not (tmp_path / "out").exists()


def test_failed_write_leaves_no_temporary_file(tmp_path, monkeypatch):
    target = tmp_path / "out" / "b.json"

    def broken(self, target_path):
        assert (self.name, target_path) == ("b.json.tmp", target)
        assert self.read_text() == "x"
        raise OSError("rename failed")

    monkeypatch.setattr(Path, "replace", broken)
    with pytest.raises(OSError, match="rename failed"):
        baseline._write(target, "x")
    assert list(target.parent.iterdir()) == []


@pytest.mark.parametrize(
    "argv",
    [
        ("kubectl", "get", "secret", "x", "-o", "jsonpath={.data}"),
        ("kubectl", "get", "secrets"),
        ("kubectl", "describe", "secret/x"),
        ("kubectl", "get", "secrets/x"),
        ("kubectl", "get", "pods,secrets"),
        ("kubectl", "get", "--raw=/api/v1/namespaces/n/secrets"),
    ],
)
def test_read_only_refuses_secret_reads(argv):
    assert baseline.read_only(argv) is False
    assert baseline.read_only(tuple(a.replace("secret", "configmap") for a in argv)) is True


def test_sanitize_redacts_ssh_auth_failures():
    assert baseline.sanitize("ops@anton-k3s.internal: Permission denied") == "<redacted-url> Permission denied"


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
                "unknown_live": ["api", "x"],
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
        "baseline_drift_items counts drift found by this run; the Drift section keeps every earlier entry.\n"
        f"Regenerate with `{baseline.REGENERATE}`.\n"
        "\n"
        "| Repository | Source | Deployed | Unknown live values |\n"
        "|---|---|---|---|\n"
        "| a | dev `c1` | rev: verified `c1`, matches source; old: verified `c0`, differs from source; "
        "img: verified `i:1`; api: unverified (a\\|b c) | api, x |\n"
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


def test_utc_now_format(monkeypatch):
    class Clock:
        @staticmethod
        def now(tz):
            assert tz is UTC
            return datetime(2026, 10, 7, 15, 4, 5, tzinfo=tz)

    monkeypatch.setattr(baseline, "datetime", Clock)
    assert baseline._utc_now() == "2026-10-07T15:04:05Z"


def test_cli_writes_json_and_markdown_and_reads_previous(planted, tmp_path, monkeypatch):
    sources, _ = planted
    out_json = tmp_path / "out" / "deep" / "baseline.json"
    out_md = tmp_path / "out" / "deep" / "baseline.md"
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
