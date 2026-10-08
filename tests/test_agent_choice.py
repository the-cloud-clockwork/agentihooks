import time

import pytest

from scripts import agent_choice, herdr_host, init_agent


@pytest.fixture(autouse=True)
def _no_herdr(monkeypatch):
    monkeypatch.setattr(herdr_host, "binary", lambda: None)


def test_an_explicit_agent_never_falls_through():
    assert agent_choice.choose("codex", {}) == ("codex", "requested")


def test_the_rotation_pick_names_the_harness_with_the_fewest_sessions(monkeypatch):
    from scripts.swarm import capacity

    rows = [
        capacity.Account("claude", "a", "OPEN", 2, 90, 90, 3),
        capacity.Account("codex", "cx", "OPEN", 0, 90, 90, 3),
    ]
    seen = []
    monkeypatch.setattr(capacity, "accounts", lambda environ, now: seen.append((environ, now)) or rows)
    assert agent_choice.choose("", {"A": "1"}) == ("codex", "rotation")
    assert seen[0][0] == {"A": "1"} and abs(seen[0][1] - time.time()) < 60


def test_no_free_seat_gives_claude_all_full(monkeypatch):
    from scripts.swarm import capacity

    monkeypatch.setattr(
        capacity, "accounts", lambda environ, now: [capacity.Account("claude", "a", "CLOSED", 3, 0, 0, 3)]
    )
    assert agent_choice.choose("", {}) == ("claude", agent_choice.ALL_FULL)


def test_init_agent_reports_the_chosen_agent(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(
        agent_choice, "choose", lambda requested, environ: ("codex", "fallthrough: claude has no quota")
    )
    monkeypatch.setattr(init_agent.shutil, "which", lambda name: None)
    monkeypatch.setattr(
        init_agent,
        "_launch_command",
        lambda launcher, directory, title, environ: ("linux", ["/usr/bin/terminal", str(launcher)]),
    )
    rc = init_agent.main(
        ["--dir", str(tmp_path), "--dry-run"], {"HOME": str(tmp_path), "XDG_RUNTIME_DIR": str(tmp_path / "rt")}
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "agent=codex" in out and "agent_reason=fallthrough: claude has no quota" in out


def test_a_codex_launcher_runs_through_the_codex_router(monkeypatch, tmp_path):
    monkeypatch.setattr(init_agent.shutil, "which", lambda name: f"/usr/bin/{name}")
    env = {
        "HOME": str(tmp_path),
        "XDG_RUNTIME_DIR": str(tmp_path / "rt"),
        "SHELL": "/bin/bash",
        "AGENTIHOOKS_TRUST_LAUNCH_DIR": "0",
    }
    launcher, prompt_file = init_agent._write_launcher(
        tmp_path, "eng-c", "fix it", ["--model", "o3"], env, init_agent.AgentSpec(agent="codex")
    )
    text = launcher.read_text()
    report = launcher.with_suffix(".route")
    assert (
        f"/usr/bin/agentihooks codex --agentihooks-report {report} -c 'model_reasoning_effort=\"high\"' --model o3 "
        f'"$(cat {prompt_file})"'
    ) in text
    assert "status=direct" not in text and "agentihooks claude" not in text


def test_a_handoff_stays_on_claude(monkeypatch, tmp_path, capsys):
    rc = init_agent.main(
        ["--dir", str(tmp_path), "--agent", "codex", "--handoff", "--prompt", "doc", "--dry-run"],
        {"HOME": str(tmp_path), "XDG_RUNTIME_DIR": str(tmp_path / "rt")},
    )
    assert rc == 2
    assert "unsupported quota transfer" in capsys.readouterr().err


def test_one_claude_account_quota_comes_from_that_account_in_the_router_cache(monkeypatch):
    from scripts.claude_quota_balancer import ProbeResult, QuotaWindow

    def result(account, left):
        return ProbeResult(account, "allowed", "NORMAL", left, QuotaWindow(used=100 - left), QuotaWindow(used=0))

    now = time.time()
    seen = [(now, result("a", 2.0)), (now, result("b", 40.0)), (now - 901, result("old", 90.0))]
    monkeypatch.setattr("scripts.claude_quota_balancer.cached_observations", lambda: seen)
    assert agent_choice.account_has_quota("claude", "a", {}) is False
    assert agent_choice.account_has_quota("claude", "b", {}) is True
    assert agent_choice.account_has_quota("claude", "c", {}) is None
    assert agent_choice.account_has_quota("claude", "old", {}) is None


def test_one_codex_account_quota_uses_its_week_band(monkeypatch):
    from scripts import codex_router
    from scripts.claude_quota_balancer import QuotaWindow
    from scripts.codex_quota import CodexQuota

    now = time.time()
    readings = {"seen": CodexQuota(observed_at=now, plan_type="pro", seven_day=QuotaWindow(used=95.1))}
    monkeypatch.setattr("scripts.codex_quota.latest_codex_quota", lambda environ=None, keep=None: readings["seen"])
    assert agent_choice.account_has_quota("codex", codex_router.CODEX_DEFAULT, {}) is False
    readings["seen"] = CodexQuota(observed_at=now, plan_type="pro", seven_day=QuotaWindow(used=95.0))
    assert agent_choice.account_has_quota("codex", codex_router.CODEX_DEFAULT, {}) is True
    readings["seen"] = CodexQuota(observed_at=now - 901, plan_type="pro", seven_day=QuotaWindow(used=50.0))
    assert agent_choice.account_has_quota("codex", codex_router.CODEX_DEFAULT, {}) is None
    assert agent_choice.account_has_quota("codex", "nobody", {}) is None


@pytest.mark.parametrize(
    ("reason", "kind"),
    [
        ("requested", "forced"),
        ("rotation", "rotation"),
        ("fallthrough: claude has no placeable quota seats", "overflow"),
        ("every account is at its session cap", "other"),
    ],
)
def test_each_router_reason_names_its_choice_kind(reason, kind):
    assert agent_choice.choice_kind(reason) == kind
