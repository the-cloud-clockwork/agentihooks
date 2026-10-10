import base64
import json
import os
import subprocess
import time
from datetime import UTC, datetime

from hooks.context import account_sessions
from scripts import agents_quota, codex_quota, codex_router
from scripts.claude_quota_balancer import QuotaWindow
from scripts.codex_quota import CodexQuota
from scripts.codex_router import CodexAccount

NOW = 1_800_000_000
DEPLETED = "workspace_owner_credits_depleted"
ALPHA = "11111111-1111-1111-1111-111111111111"
BETA = "22222222-2222-2222-2222-222222222222"


def _stamp(at: float) -> str:
    return datetime.fromtimestamp(at, UTC).isoformat().replace("+00:00", "Z")


def _event(at: float, limits: dict) -> str:
    return json.dumps(
        {"timestamp": _stamp(at), "type": "event_msg", "payload": {"type": "token_count", "rate_limits": limits}}
    )


def _windows(five: float, week: float, five_reset: int, week_reset: int, limit_id: str = "codex") -> dict:
    return {
        "limit_id": limit_id,
        "primary": {"used_percent": five, "window_minutes": 300, "resets_at": five_reset},
        "secondary": {"used_percent": week, "window_minutes": 10080, "resets_at": week_reset},
        "plan_type": "pro",
    }


def _depleted(limit_id: str = "premium") -> dict:
    return {
        "limit_id": limit_id,
        "primary": None,
        "secondary": None,
        "credits": {"has_credits": False, "unlimited": False, "balance": None},
        "plan_type": None,
        "rate_limit_reached_type": DEPLETED,
    }


def _rollout(home, session_id: str, lines: list[str], mtime: float):
    path = home / ".codex" / "sessions" / "2026" / "10" / "11" / f"rollout-2026-10-11T00-00-00-{session_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")
    os.utime(path, (mtime, mtime))
    return path


def _proc(root, pid, comm, ppid, argv, env):
    d = root / str(pid)
    d.mkdir(parents=True)
    (d / "comm").write_text(comm + "\n")
    (d / "stat").write_text(f"{pid} ({comm}) S {ppid} 0 0\n")
    (d / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
    (d / "environ").write_bytes(b"\0".join(f"{k}={v}".encode() for k, v in env.items()) + b"\0")


def _jwt(account_id: str) -> str:
    claims = {codex_router.OPENAI_AUTH_CLAIM: {"chatgpt_account_id": account_id}}
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"e30.{body}.sig"


def test_a_reached_limit_event_is_a_blocked_reading_that_names_its_reason():
    quota = codex_quota.parse_event(_event(NOW, _depleted()))
    assert quota == CodexQuota(observed_at=NOW, plan_type="?", reached=DEPLETED)
    assert quota.state == "BLOCKED"
    assert quota.highest_used is None


def test_a_reached_limit_keeps_the_windows_it_carries():
    limits = {**_windows(100.0, 40.0, NOW + 60, NOW + 6000), "rate_limit_reached_type": "rate_limit_reached"}
    quota = codex_quota.parse_event(_event(NOW, limits))
    assert quota.reached == "rate_limit_reached"
    assert quota.five_hour == QuotaWindow(used=100.0, resets_at=NOW + 60)
    assert quota.seven_day == QuotaWindow(used=40.0, resets_at=NOW + 6000)
    assert quota.state == "BLOCKED"


def test_windows_are_read_only_from_the_default_codex_limit_family():
    other = codex_quota.parse_event(_event(NOW, _windows(10.0, 20.0, NOW + 60, NOW + 6000, "codex_other")))
    assert other is None
    unnamed = {key: value for key, value in _windows(10.0, 20.0, NOW + 60, NOW + 6000).items() if key != "limit_id"}
    quota = codex_quota.parse_event(_event(NOW, unnamed))
    assert (quota.five_hour.used, quota.seven_day.used, quota.reached) == (10.0, 20.0, "")
    assert quota.state == "NORMAL"


def test_a_fresh_blocked_reading_takes_no_session_and_a_stale_one_is_unknown():
    blocked = CodexQuota(observed_at=NOW, plan_type="?", reached=DEPLETED)
    assert codex_router.account_cap(blocked, NOW) == 0
    assert codex_router.account_cap(blocked, NOW + 901) is None
    full = CodexQuota(NOW, "pro", seven_day=QuotaWindow(used=10.0, resets_at=NOW + 6000), reached=DEPLETED)
    assert codex_router.account_cap(full, NOW) == 0


def test_the_newest_depleted_event_replaces_an_older_windowed_reading(tmp_path):
    lines = [_event(NOW - 600, _windows(98.0, 15.0, NOW + 60, NOW + 6000)), _event(NOW, _depleted())]
    _rollout(tmp_path, ALPHA, lines, mtime=NOW)
    quota = codex_quota.latest_codex_quota({"HOME": str(tmp_path)})
    assert (quota.observed_at, quota.reached, quota.five_hour.used) == (NOW, DEPLETED, None)


def test_a_routed_launch_counts_by_its_subcommand_after_the_router_options(tmp_path):
    root = tmp_path / "proc"
    alpha = {"AH_CX_TOKEN_alpha": "a", "CODEX_ACCESS_TOKEN": "a"}
    bearer = ["-c", 'model_provider="agentihooks-chatgpt"', "--config", "x=1"]
    _proc(root, 400, "codex", 1, ["codex", "--no-daemon", *bearer], alpha)
    _proc(root, 401, "codex", 1, ["codex", "--no-daemon", *bearer, "exec", "--json", "ok"], alpha)
    _proc(root, 402, "codex", 1, ["codex", "--no-daemon", "-c", "a=1", "app-server"], alpha)
    _proc(root, 403, "codex", 1, ["codex", "--no-daemon", "-c", "a=1", "mcp-server"], alpha)
    _proc(root, 404, "codex", 1, ["codex", "--no-daemon", "-c"], alpha)
    _proc(root, 405, "codex", 1, ["codex", "-c=exec", "resume"], {})
    assert account_sessions.codex_sessions_by_account(root) == {"alpha": 2, "default": 1}
    assert account_sessions.live_codex_sessions(root) == 3


def test_rows_say_stale_unknown_and_blocked_and_never_carry_a_stale_cap():
    accounts = [CodexAccount("default"), CodexAccount("alpha", "AH_CX_TOKEN_alpha"), CodexAccount("beta", "x")]
    stale = CodexQuota(NOW - 901, "pro", QuotaWindow(10.0, NOW + 60), QuotaWindow(20.0, NOW + 6000))
    blocked = CodexQuota(NOW - 120, "?", reached=DEPLETED)
    rows = agents_quota.codex_rows(accounts, {"default": stale, "alpha": blocked}, {"alpha": 1}, NOW)
    assert [(row.account, row.state, row.cap, row.sessions) for row in rows] == [
        ("default", "STALE", None, 0),
        ("alpha", "BLOCKED", 0, 1),
        ("beta", "UNKNOWN", None, 0),
    ]
    assert rows[0].source == "session-log 15m ago"
    assert rows[1].source == f"session-log 2m ago, {DEPLETED}"
    assert rows[2].source == "no session log"


def test_a_window_whose_reset_has_passed_reads_full():
    quota = CodexQuota(NOW - 60, "pro", QuotaWindow(97.0, NOW - 1), QuotaWindow(40.0, NOW + 6000))
    [row] = agents_quota.codex_rows([CodexAccount("default")], {"default": quota}, {}, NOW)
    assert (row.five_hour_left, row.seven_day_left) == (100.0, 60.0)
    assert (row.five_hour_resets_at, row.seven_day_resets_at, row.observed_at) == (NOW - 1, NOW + 6000, NOW - 60)


def test_the_account_open_placement_picks_next_is_marked(monkeypatch):
    rows = [
        agents_quota.QuotaRow("codex", "alpha", "NORMAL", 0, 90.0, 80.0, None, "log", cap=6),
        agents_quota.QuotaRow("codex", "beta", "NORMAL", 0, 90.0, 80.0, None, "log", cap=6),
    ]
    marked = agents_quota.mark_next(rows, "beta")
    assert [row.selected for row in marked] == [False, True]
    assert agents_quota.mark_next(rows, "") == rows
    table = agents_quota.render(marked, now=NOW).splitlines()
    assert table[1].split()[:2] == ["codex", "alpha"]
    assert table[2].split()[:3] == ["codex", "beta", agents_quota.NEXT_MARK]


def test_next_account_is_open_placement_over_unrefused_accounts(monkeypatch):
    env = {"AH_CX_TOKEN_alpha": "sk-refused", "AH_CX_TOKEN_beta": "at-beta"}
    pool = [CodexAccount("alpha", "AH_CX_TOKEN_alpha"), CodexAccount("beta", "AH_CX_TOKEN_beta")]
    quotas = {name: CodexQuota(NOW, "pro", seven_day=QuotaWindow(10.0, NOW + 6000)) for name in ("alpha", "beta")}
    assert agents_quota.next_account(pool, quotas, {"beta": 5}, NOW, env) == "beta"
    assert agents_quota.next_account(pool, quotas, {"beta": 6}, NOW, env) == ""
    assert agents_quota.next_account(pool, {}, {}, NOW, env) == ""


def test_two_concurrent_accounts_read_count_and_place_apart(tmp_path, monkeypatch):
    now = time.time()
    home = tmp_path / "h"
    _rollout(home, ALPHA, [_event(now - 60, _windows(20.0, 97.0, int(now) + 3600, int(now) + 86400))], mtime=now - 60)
    _rollout(home, BETA, [_event(now - 30, _windows(70.0, 40.0, int(now) + 1800, int(now) + 400000))], mtime=now - 30)
    monkeypatch.setattr(codex_router, "_registry", lambda: {ALPHA: {"account": "alpha"}, BETA: {"account": "beta"}})
    root = tmp_path / "proc"
    _proc(root, 500, "codex", 1, ["codex", "--no-daemon", "-c", "a=1"], {"AH_CX_TOKEN_alpha": "x"})
    _proc(root, 501, "codex", 1, ["codex", "--no-daemon", "-c", "a=1", "exec", "ok"], {"AH_CX_TOKEN_beta": "x"})
    _proc(root, 502, "codex", 1, ["codex", "--no-daemon", "-c", "a=1"], {"AH_CX_TOKEN_beta": "x"})
    _proc(root, 503, "codex", 1, ["codex", "--no-daemon", "-c", "a=1"], {"AH_CX_TOKEN_beta": "x"})
    monkeypatch.setattr(
        codex_router, "codex_sessions_by_account", lambda: account_sessions.codex_sessions_by_account(root)
    )
    env = {"HOME": str(home), "AH_CX_TOKEN_alpha": _jwt("acct-a"), "AH_CX_TOKEN_beta": _jwt("acct-b")}
    probes = []

    def run(argv, **kwargs):
        probes.append(argv)
        return subprocess.CompletedProcess(argv, 1, "", "")

    pool = [CodexAccount("alpha", "AH_CX_TOKEN_alpha"), CodexAccount("beta", "AH_CX_TOKEN_beta")]
    readings = codex_router.quotas(pool, env)
    assert (readings["alpha"].seven_day.used, readings["alpha"].seven_day.resets_at) == (97.0, int(now) + 86400)
    assert (readings["beta"].seven_day.used, readings["beta"].five_hour.resets_at) == (40.0, int(now) + 1800)
    assert codex_router.codex_sessions_by_account() == {"alpha": 1, "beta": 2}
    launched = {}
    status = codex_router.main(["exec", "ok"], env, lambda *args: launched.update(argv=args[1], env=args[2]), run)
    assert status == 0
    assert launched["env"]["AH_CX_TOKEN_beta"] == env["AH_CX_TOKEN_beta"]
    assert "AH_CX_TOKEN_alpha" not in launched["env"]
    assert launched["env"][codex_router.ACCOUNT_ID_ENV] == "acct-b"
    assert launched["argv"][-2:] == ["exec", "ok"]
    assert [argv[-2:] for argv in probes] == [["login", "status"]]
