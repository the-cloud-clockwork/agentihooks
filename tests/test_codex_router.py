import subprocess

import pytest

from scripts import codex_router as router
from scripts.claude_quota_balancer import QuotaWindow
from scripts.codex_quota import CodexQuota

ENV = {
    "HOME": "/home/u",
    "AH_CX_TOKEN_alpha": "cx-value-a",
    "AH_CX_TOKEN_beta": "cx-value-b",
    "AH_CC_TOKEN_ncgma": "cc-value",
    "CODEX_ACCESS_TOKEN": "stale-value",
}


def _quota(used):
    return CodexQuota(observed_at=0, plan_type="team", seven_day=QuotaWindow(used=used))


def _status(returncode):
    def run(argv, **kwargs):
        assert argv[-2:] == ["login", "status"]
        assert "CODEX_ACCESS_TOKEN" not in kwargs["env"]
        assert not any(name.startswith("AH_CX_TOKEN_") for name in kwargs["env"])
        return subprocess.CompletedProcess(argv, returncode, "", "")

    return run


def _accounts(signed_in=True):
    return [
        router.CodexAccount("default", signed_in=signed_in),
        router.CodexAccount("alpha", "AH_CX_TOKEN_alpha"),
        router.CodexAccount("beta", "AH_CX_TOKEN_beta"),
    ]


def test_accounts_are_the_default_login_and_every_token_variable():
    assert router.accounts(ENV, run=_status(0)) == _accounts()
    assert router.accounts(ENV, run=_status(1))[0] == router.CodexAccount("default", signed_in=False)


def test_the_most_routing_left_below_the_cap_wins():
    quotas = {"default": _quota(80.0), "alpha": _quota(30.0), "beta": _quota(10.0)}
    assert router.select(_accounts(), quotas, {"beta": 3}, cap=3) == (_accounts()[1], "open")
    assert router.select(_accounts(), quotas, {}, cap=3) == (_accounts()[2], "open")


def test_every_account_at_the_cap_overflows_to_the_least_loaded():
    quotas = {"default": _quota(10.0), "alpha": _quota(20.0), "beta": _quota(30.0)}
    sessions = {"default": 5, "alpha": 3, "beta": 4}
    assert router.select(_accounts(), quotas, sessions, cap=3) == (_accounts()[1], "overflow")


def test_a_signed_out_default_or_a_spent_account_is_never_picked():
    quotas = {"default": None, "alpha": _quota(99.0), "beta": None}
    assert router.select(_accounts(signed_in=False), quotas, {}, cap=3) == (_accounts()[2], "open")
    with pytest.raises(router.RoutingError):
        router.select(_accounts(signed_in=False)[:2], quotas, {}, cap=3)


def test_a_forced_route_ignores_quota_and_cap():
    quotas = {"default": _quota(1.0), "alpha": _quota(99.0), "beta": _quota(1.0)}
    assert router.select(_accounts(), quotas, {"alpha": 9}, cap=3, route="alpha") == (_accounts()[1], "forced")
    with pytest.raises(router.RoutingError, match="available: default, alpha, beta"):
        router.select(_accounts(), quotas, {}, cap=3, route="gamma")


def test_a_token_child_keeps_only_its_token_and_runs_without_the_shared_daemon():
    child = router.child_environment(_accounts()[1], ENV)
    assert child["CODEX_ACCESS_TOKEN"] == "cx-value-a"
    assert child["AH_CX_TOKEN_alpha"] == "cx-value-a"
    assert "AH_CX_TOKEN_beta" not in child and "AH_CC_TOKEN_ncgma" not in child
    assert router.command(_accounts()[1], "/usr/bin/codex", ["-m", "o3"]) == [
        "/usr/bin/codex",
        "--no-daemon",
        "-m",
        "o3",
    ]


def test_the_default_child_runs_on_the_stored_login_with_no_token():
    child = router.child_environment(_accounts()[0], ENV)
    assert not any(name.startswith(("AH_CX_TOKEN_", "AH_CC_TOKEN_", "CODEX_ACCESS_TOKEN")) for name in child)
    assert child["HOME"] == "/home/u"
    assert router.command(_accounts()[0], "/usr/bin/codex", ["-m", "o3"]) == ["/usr/bin/codex", "-m", "o3"]


def _launch(monkeypatch, tmp_path, environ, argv, quotas=None, sessions=None):
    seen = {}
    monkeypatch.setattr(router, "codex_sessions_by_account", lambda: sessions or {})
    monkeypatch.setattr(router, "quotas", lambda accounts, environ: quotas or {})
    monkeypatch.setattr(router.shutil, "which", lambda name: f"/usr/bin/{name}")

    def execvpe(path, cmd, env):
        seen.update(path=path, cmd=cmd, env=env)

    report = tmp_path / "launch.route"
    rc = router.main(
        ["--agentihooks-report", str(report), *argv],
        environ={**environ, "HOME": str(tmp_path)},
        execvpe=execvpe,
        run=_status(0),
    )
    return rc, seen, report


def test_a_routed_launch_reports_the_account_and_never_prints_a_token(monkeypatch, tmp_path, capsys):
    quotas = {"default": _quota(90.0), "alpha": _quota(10.0), "beta": _quota(50.0)}
    rc, seen, report = _launch(monkeypatch, tmp_path, ENV, ["-m", "o3"], quotas)
    assert rc == 0
    assert seen["cmd"] == ["/usr/bin/codex", "--no-daemon", "-m", "o3"]
    assert seen["env"]["CODEX_ACCESS_TOKEN"] == "cx-value-a"
    assert report.read_text() == "status=routed\naccount=alpha\nplacement=open\n"
    out = capsys.readouterr()
    assert "account=alpha" in out.out
    assert "cx-value" not in out.out + out.err


def test_route_forces_an_account(monkeypatch, tmp_path):
    rc, seen, report = _launch(monkeypatch, tmp_path, ENV, ["--route", "beta", "-m", "o3"])
    assert rc == 0 and seen["env"]["CODEX_ACCESS_TOKEN"] == "cx-value-b"
    assert seen["cmd"] == ["/usr/bin/codex", "--no-daemon", "-m", "o3"]
    assert "account=beta\nplacement=forced" in report.read_text()


def test_no_routable_account_fails_the_launch_and_reports_it(monkeypatch, tmp_path, capsys):
    quotas = {"default": _quota(99.0), "alpha": _quota(99.0), "beta": _quota(99.0)}
    rc, seen, report = _launch(monkeypatch, tmp_path, ENV, [], quotas)
    assert rc == 3 and not seen
    assert report.read_text().startswith("status=failed\n")
    assert "cx-value" not in capsys.readouterr().err


def test_without_token_variables_codex_launches_as_today(monkeypatch, tmp_path):
    def no_status(argv, **kwargs):
        raise AssertionError("no login status check without tokens")

    monkeypatch.setattr(router, "codex_sessions_by_account", lambda: {"default": 9})
    monkeypatch.setattr(router.shutil, "which", lambda name: f"/usr/bin/{name}")
    seen = {}
    report = tmp_path / "launch.route"
    rc = router.main(
        ["--agentihooks-report", str(report), "-m", "o3", "do it"],
        environ={"HOME": str(tmp_path), "AH_CC_TOKEN_ncgma": "cc-value"},
        execvpe=lambda path, cmd, env: seen.update(cmd=cmd, env=env),
        run=no_status,
    )
    assert rc == 0
    assert seen["cmd"] == ["/usr/bin/codex", "-m", "o3", "do it"]
    assert "CODEX_ACCESS_TOKEN" not in seen["env"]
    assert report.read_text() == "status=routed\naccount=default\nplacement=open\n"
