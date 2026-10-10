"""Tests for hooks.context.account_sessions — live sessions per account from /proc."""

from unittest.mock import patch

from hooks.context import account_sessions as acc


def _proc(root, pid, comm, ppid, argv, env):
    d = root / str(pid)
    d.mkdir(parents=True)
    (d / "comm").write_text(comm + "\n")
    (d / "stat").write_text(f"{pid} ({comm}) S {ppid} 0 0\n")
    (d / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
    (d / "environ").write_bytes(b"\0".join(f"{k}={v}".encode() for k, v in env.items()) + b"\0")


def _tree(tmp_path):
    root = tmp_path / "proc"
    _proc(
        root,
        100,
        "claude",
        1,
        ["/home/u/.local/bin/claude", "--dangerously-skip-permissions"],
        {"AH_CC_TOKEN_alpha": "secret-a", "CLAUDE_CODE_OAUTH_TOKEN": "secret-a"},
    )
    _proc(root, 101, "claude", 1, ["claude", "--resume", "x"], {"AH_CC_TOKEN_alpha": "secret-a"})
    _proc(root, 102, "claude", 1, ["claude"], {"AH_CC_TOKEN_beta": "secret-b"})
    _proc(root, 103, "claude", 1, ["claude"], {"HOME": "/home/u"})
    _proc(root, 104, "claude", 100, ["claude", "-p", "Reply with exactly OK."], {})
    _proc(root, 105, "bash", 100, ["bash", "-c", "cd x && python -m hooks"], {"AH_CC_TOKEN_alpha": "secret-a"})
    _proc(root, 106, "python3", 105, ["python3", "-m", "hooks"], {"AH_CC_TOKEN_alpha": "secret-a"})
    _proc(
        root,
        107,
        "node",
        1,
        ["node", "/usr/lib/node_modules/@anthropic-ai/claude-code/cli.js"],
        {"AH_CC_TOKEN_beta": "secret-b"},
    )
    return root


def test_account_comes_from_the_single_token_name():
    assert acc.account_from_names(["HOME", "AH_CC_TOKEN_alpha"]) == "alpha"
    assert acc.account_from_names(["AH_CC_TOKEN_alpha", "AH_CC_TOKEN_beta"]) == acc.UNROUTED
    assert acc.account_from_names(["HOME"]) == acc.UNROUTED
    assert acc.environment_account({"AH_CC_TOKEN_alpha": ""}) == acc.UNROUTED


def test_the_route_marker_attributes_an_api_session():
    assert acc.account_from_names(iter(["HOME", acc.API_MARKER])) == acc.API_ACCOUNT == "api"
    assert acc.account_from_names([acc.API_MARKER, "AH_CC_TOKEN_alpha"]) == "api"
    assert acc.environment_account({acc.API_MARKER: "1", "HOME": "/home/u"}) == "api"
    assert acc.environment_account({acc.API_MARKER: "", "AH_CC_TOKEN_alpha": "a"}) == "alpha"
    assert acc.codex_account_from_names([acc.API_MARKER]) == "api"
    assert acc.codex_account_from_names([acc.API_MARKER, "AH_CX_TOKEN_one"]) == "api"


def test_live_api_sessions_count_under_the_api_account(tmp_path):
    root = tmp_path / "proc"
    _proc(root, 200, "claude", 1, ["claude"], {acc.API_MARKER: "1", "HOME": "/home/u"})

    assert acc.live_sessions(root) == {200: "api"}


def test_agent_pid_skips_the_hook_shell(tmp_path):
    root = _tree(tmp_path)

    assert acc.agent_pid(105, root) == 100
    assert acc.agent_pid(106, root) == 106
    assert acc.agent_pid(999, root) == 999


def test_live_sessions_count_interactive_claude_only(tmp_path):
    root = _tree(tmp_path)

    sessions = acc.live_sessions(root)

    assert sessions == {100: "alpha", 101: "alpha", 102: "beta", 103: acc.UNROUTED, 107: "beta"}
    assert "secret" not in repr(sessions)


def test_a_process_that_exits_mid_scan_is_skipped(tmp_path):
    root = _tree(tmp_path)
    (root / "108").mkdir()

    assert 108 not in acc.live_sessions(root)
    assert acc._cmdline(108, root) == []


def test_handed_off_sessions_free_their_slot(tmp_path):
    root = _tree(tmp_path)

    with patch.object(acc, "handed_off_pids", return_value={101}):
        counts = acc.sessions_by_account(root)

    assert counts == {"alpha": 1, "beta": 2, acc.UNROUTED: 1}


def test_session_account_reads_the_agent_process(tmp_path):
    root = _tree(tmp_path)

    assert acc.session_account(102, root) == "beta"


def test_live_codex_sessions_count_interactive_codex_only(tmp_path):
    root = tmp_path / "proc"
    _proc(root, 200, "codex", 1, ["/home/u/.local/bin/codex"], {})
    _proc(root, 201, "codex", 1, ["/home/u/.codex/packages/app-server-daemon/releases/1/bin/codex"], {})
    _proc(root, 202, "codex-code-mode", 200, ["codex-code-mode"], {})
    _proc(root, 203, "codex", 1, ["codex", "exec", "summarize"], {})
    _proc(root, 204, "codex", 1, ["codex", "--yolo"], {})
    assert acc.live_codex_sessions(root) == 2


def test_codex_sessions_count_per_codex_account(tmp_path):
    root = tmp_path / "proc"
    _proc(root, 300, "codex", 1, ["codex", "--no-daemon"], {"AH_CX_TOKEN_alpha": "cx-a", "CODEX_ACCESS_TOKEN": "cx-a"})
    _proc(root, 301, "codex", 1, ["codex", "--no-daemon"], {"AH_CX_TOKEN_alpha": "cx-a"})
    _proc(root, 302, "codex", 1, ["codex"], {"AH_CC_TOKEN_ncgma": "cc"})
    _proc(root, 303, "codex", 1, ["codex"], {"AH_CX_TOKEN_alpha": "cx-a", "AH_CX_TOKEN_beta": "cx-b"})
    _proc(root, 304, "codex", 1, ["codex", "exec", "x"], {"AH_CX_TOKEN_beta": "cx-b"})
    _proc(root, 305, "claude", 1, ["claude"], {"AH_CC_TOKEN_alpha": "cc"})
    assert acc.codex_sessions_by_account(root) == {"alpha": 2, "default": 2}
    assert acc.session_account(300, root) == "alpha"
    assert acc.session_account(302, root) == "default"
    assert acc.session_account(305, root) == "alpha"
