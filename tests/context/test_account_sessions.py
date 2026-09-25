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


def test_handed_off_sessions_free_their_slot(tmp_path):
    root = _tree(tmp_path)

    with patch.object(acc, "handed_off_pids", return_value={101}):
        counts = acc.sessions_by_account(root)

    assert counts == {"alpha": 1, "beta": 2, acc.UNROUTED: 1}


def test_max_sessions_reads_the_env_var():
    assert acc.max_sessions({}) == 2
    assert acc.max_sessions({acc.MAX_SESSIONS_ENV: "3"}) == 3
    assert acc.max_sessions({acc.MAX_SESSIONS_ENV: "0"}) == 1
    assert acc.max_sessions({acc.MAX_SESSIONS_ENV: "many"}) == 2


def test_session_account_reads_the_agent_process(tmp_path):
    root = _tree(tmp_path)

    assert acc.session_account(102, root) == "beta"
