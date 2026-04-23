"""Unit tests for hooks.context.memory_sync_events — v5 event-driven hooks."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from hooks.context import memory_sync_events as mse
from scripts import memory_mirror_sync as mm


# ---------------------------------------------------------------------------
# pull_only() — inline CLI action behind UserPromptSubmit.
# ---------------------------------------------------------------------------


def test_pull_only_skips_when_role_off(monkeypatch):
    """No git ops when role=off — early return."""
    monkeypatch.setattr(mm, "_role", lambda: "off")
    calls: list[str] = []
    monkeypatch.setattr(mm, "ensure_mirror_repo", lambda: calls.append("ensure") or Path("/x"))
    monkeypatch.setattr(mm, "fetch_remote", lambda: calls.append("fetch"))
    monkeypatch.setattr(mm, "consume_main", lambda: calls.append("consume"))
    mm.pull_only()
    assert calls == []


def test_pull_only_skips_when_remote_unset(monkeypatch):
    monkeypatch.setattr(mm, "_role", lambda: "consumer")
    monkeypatch.setattr(mm.config, "MEMORY_MIRROR_REMOTE", "")
    calls: list[str] = []
    monkeypatch.setattr(mm, "ensure_mirror_repo", lambda: calls.append("ensure") or Path("/x"))
    mm.pull_only()
    assert calls == []


def test_pull_only_runs_fetch_and_consume_for_consumer(monkeypatch):
    monkeypatch.setattr(mm, "_role", lambda: "consumer")
    monkeypatch.setattr(mm.config, "MEMORY_MIRROR_REMOTE", "git@github.com:o/r.git")
    calls: list[str] = []
    monkeypatch.setattr(mm, "ensure_mirror_repo", lambda: calls.append("ensure") or Path("/x"))
    monkeypatch.setattr(mm, "fetch_remote", lambda: calls.append("fetch"))
    monkeypatch.setattr(mm, "consume_main", lambda: calls.append("consume"))
    mm.pull_only()
    assert calls == ["ensure", "fetch", "consume"]


# ---------------------------------------------------------------------------
# on_user_prompt — fires fire-and-forget pull for active roles.
# ---------------------------------------------------------------------------


def _patch_role(monkeypatch, role: str, remote: str = "git@github.com:o/r.git"):
    from hooks import config as _cfg

    monkeypatch.setattr(_cfg, "MEMORY_MIRROR_ROLE", role)
    monkeypatch.setattr(_cfg, "MEMORY_MIRROR_REMOTE", remote)


def test_on_user_prompt_forks_pull_for_consumer(monkeypatch):
    _patch_role(monkeypatch, "consumer")
    calls: list[dict] = []

    def fake_fork(func, *args, **kwargs):
        calls.append({"func": func, "args": args, "kwargs": kwargs})

    monkeypatch.setattr(mse, "_fork_and_call", fake_fork)
    mse.on_user_prompt({"session_id": "abc"})
    assert len(calls) == 1
    assert calls[0]["func"].__name__ == "pull_only"
    assert calls[0]["kwargs"].get("timeout_sec") == 120
    assert calls[0]["kwargs"].get("task_name") == "pull"


def test_on_user_prompt_noop_when_role_off(monkeypatch):
    _patch_role(monkeypatch, "off")
    calls: list[dict] = []
    monkeypatch.setattr(mse, "_fork_and_call", lambda *a, **kw: calls.append((a, kw)))
    mse.on_user_prompt({})
    assert calls == []


def test_on_user_prompt_noop_when_remote_unset(monkeypatch):
    _patch_role(monkeypatch, "consumer", remote="")
    calls: list[dict] = []
    monkeypatch.setattr(mse, "_fork_and_call", lambda *a, **kw: calls.append((a, kw)))
    mse.on_user_prompt({})
    assert calls == []


# ---------------------------------------------------------------------------
# on_post_tool — marks dirty only when Write/Edit into memory dir.
# ---------------------------------------------------------------------------


def test_on_post_tool_marks_dirty_on_memory_write(monkeypatch, tmp_path):
    _patch_role(monkeypatch, "contributor")
    monkeypatch.setattr(mse, "_DIRTY_DIR", tmp_path / "memory_dirty")
    payload = {
        "tool_name": "Write",
        "tool_input": {"file_path": "/home/x/.claude/projects/abc/memory/note.md"},
        "session_id": "sess-123",
    }
    mse.on_post_tool(payload)
    flag = tmp_path / "memory_dirty" / "sess-123"
    assert flag.exists()
    assert flag.read_text() == "/home/x/.claude/projects/abc/memory/note.md"


def test_on_post_tool_ignores_non_memory_write(monkeypatch, tmp_path):
    _patch_role(monkeypatch, "contributor")
    monkeypatch.setattr(mse, "_DIRTY_DIR", tmp_path / "memory_dirty")
    mse.on_post_tool({
        "tool_name": "Write",
        "tool_input": {"file_path": "/tmp/foo.md"},
        "session_id": "sess-123",
    })
    assert not (tmp_path / "memory_dirty").exists() or not any(
        (tmp_path / "memory_dirty").iterdir()
    )


def test_on_post_tool_ignores_non_write_tool(monkeypatch, tmp_path):
    _patch_role(monkeypatch, "contributor")
    monkeypatch.setattr(mse, "_DIRTY_DIR", tmp_path / "memory_dirty")
    mse.on_post_tool({
        "tool_name": "Bash",
        "tool_input": {"command": "ls"},
        "session_id": "sess-123",
    })
    assert not (tmp_path / "memory_dirty").exists() or not any(
        (tmp_path / "memory_dirty").iterdir()
    )


def test_on_post_tool_ignores_when_role_consumer(monkeypatch, tmp_path):
    _patch_role(monkeypatch, "consumer")
    monkeypatch.setattr(mse, "_DIRTY_DIR", tmp_path / "memory_dirty")
    mse.on_post_tool({
        "tool_name": "Write",
        "tool_input": {"file_path": "/home/x/.claude/projects/abc/memory/note.md"},
        "session_id": "sess-123",
    })
    assert not (tmp_path / "memory_dirty").exists() or not any(
        (tmp_path / "memory_dirty").iterdir()
    )


# ---------------------------------------------------------------------------
# on_stop — fires propose for contributor when dirty.
# ---------------------------------------------------------------------------


def test_on_stop_forks_propose_when_dirty(monkeypatch, tmp_path):
    _patch_role(monkeypatch, "contributor")
    dirty_dir = tmp_path / "memory_dirty"
    dirty_dir.mkdir(parents=True)
    (dirty_dir / "sess-abc12345extra").write_text("/home/x/.../memory/x.md")
    monkeypatch.setattr(mse, "_DIRTY_DIR", dirty_dir)
    monkeypatch.setenv("AGENTICORE_AGENT_NAME", "publisher")

    calls: list[dict] = []
    monkeypatch.setattr(
        mse, "_fork_and_call",
        lambda func, *a, **kw: calls.append({"func": func, "args": a, "kwargs": kw}),
    )
    mse.on_stop({"session_id": "sess-abc12345extra"})

    assert len(calls) == 1
    assert calls[0]["func"].__name__ == "propose_pr"
    assert calls[0]["kwargs"]["agent_name"] == "publisher"
    # Session id is truncated to 8 chars
    assert calls[0]["kwargs"]["session_id"] == "sess-abc"
    assert calls[0]["kwargs"]["task_name"] == "propose"
    # Flag was consumed
    assert not (dirty_dir / "sess-abc12345extra").exists()


def test_on_stop_noop_when_not_dirty(monkeypatch, tmp_path):
    _patch_role(monkeypatch, "contributor")
    monkeypatch.setattr(mse, "_DIRTY_DIR", tmp_path / "memory_dirty")
    calls: list = []
    monkeypatch.setattr(mse, "_fork_and_call", lambda *a, **kw: calls.append((a, kw)))
    mse.on_stop({"session_id": "sess-xyz"})
    assert calls == []


def test_on_stop_skips_for_authority(monkeypatch, tmp_path):
    """Authority role never triggers propose from Stop — daemon owns push."""
    _patch_role(monkeypatch, "authority")
    dirty_dir = tmp_path / "memory_dirty"
    dirty_dir.mkdir(parents=True)
    (dirty_dir / "sess-abc").write_text("x")
    monkeypatch.setattr(mse, "_DIRTY_DIR", dirty_dir)

    calls: list = []
    monkeypatch.setattr(mse, "_fork_and_call", lambda *a, **kw: calls.append((a, kw)))
    mse.on_stop({"session_id": "sess-abc"})
    assert calls == []


def test_on_stop_skips_for_consumer(monkeypatch, tmp_path):
    _patch_role(monkeypatch, "consumer")
    dirty_dir = tmp_path / "memory_dirty"
    dirty_dir.mkdir(parents=True)
    (dirty_dir / "sess-abc").write_text("x")
    monkeypatch.setattr(mse, "_DIRTY_DIR", dirty_dir)

    calls: list = []
    monkeypatch.setattr(mse, "_fork_and_call", lambda *a, **kw: calls.append((a, kw)))
    mse.on_stop({"session_id": "sess-abc"})
    assert calls == []


# ---------------------------------------------------------------------------
# propose_pr v5 — branch naming uses agent_name + session_id.
# ---------------------------------------------------------------------------


def test_propose_pr_uses_new_branch_naming(monkeypatch):
    """propose_pr(session_id=..., agent_name=...) → branch memory/<agent>/<sid>."""
    monkeypatch.setattr(mm, "_role", lambda: "contributor")
    monkeypatch.setattr(
        mm.config, "MEMORY_MIRROR_REMOTE", "git@github.com:owner/repo.git"
    )
    monkeypatch.setattr(mm.shutil, "which", lambda cmd: "/usr/bin/gh")
    monkeypatch.setattr(mm, "ensure_mirror_repo", lambda: Path("/fake/mirror"))
    monkeypatch.setattr(mm, "snapshot_in", lambda: None)
    monkeypatch.setattr(mm, "fetch_remote", lambda: None)

    captured: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        captured.append(list(cmd))
        if cmd[:3] == ["git", "add", "-A"]:
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if cmd[:2] == ["git", "write-tree"]:
            return subprocess.CompletedProcess(cmd, 0, "tree-sha-aaaa\n", "")
        if cmd[:2] == ["git", "rev-parse"]:
            if "refs/remotes/origin/main" in cmd:
                return subprocess.CompletedProcess(cmd, 0, "main-sha-bbbb\n", "")
            if "^{tree}" in "".join(cmd):
                return subprocess.CompletedProcess(cmd, 0, "main-tree-ccc\n", "")
        if cmd[:3] == ["git", "diff", "--name-only"]:
            return subprocess.CompletedProcess(cmd, 0, "by-project/publisher/memory/note.md\n", "")
        if cmd[:2] == ["git", "commit-tree"]:
            return subprocess.CompletedProcess(cmd, 0, "commit-sha-dddd\n", "")
        if cmd[:2] == ["git", "push"]:
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if cmd[:2] == ["gh", "pr"]:
            return subprocess.CompletedProcess(cmd, 0, "https://github.com/owner/repo/pull/42\n", "")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(mm, "_run", fake_run)
    rc = mm.propose_pr(session_id="abc12345", agent_name="publisher")
    assert rc == 0

    # Find the push + gh pr create commands
    push_cmds = [c for c in captured if c[:2] == ["git", "push"]]
    pr_cmds = [c for c in captured if c[:2] == ["gh", "pr"]]
    assert push_cmds, "should have pushed branch"
    assert any("memory/publisher/abc12345" in arg for arg in push_cmds[0]), push_cmds[0]
    assert pr_cmds, "should have called gh pr create"
    pr_cmd = pr_cmds[0]
    assert "memory/publisher/abc12345" in pr_cmd
    title_idx = pr_cmd.index("--title")
    title = pr_cmd[title_idx + 1]
    assert "memory: publisher —" in title
    assert "1 file(s) touched" in title
