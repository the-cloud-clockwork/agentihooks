from __future__ import annotations

import asyncio
import subprocess
import sys
import time
from pathlib import Path

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from hooks.serena_router.app import SESSION_IDLE_SECONDS, build_app
from hooks.serena_router.pool import BackendConfig, Pool, probe_tools
from hooks.serena_router.server import UNBOUND, build_server, edit_tools

FAKE = Path(__file__).parent / "fixtures" / "fake_serena.py"


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> dict[str, Path]:
    primary = tmp_path / "primary"
    primary.mkdir()
    _git(primary, "init", "-q", "-b", "dev")
    _git(primary, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "init")
    for name in ("a", "b"):
        _git(primary, "worktree", "add", "-q", "-b", name, str(tmp_path / f"wt-{name}"))
    return {"primary": primary, "a": tmp_path / "wt-a", "b": tmp_path / "wt-b"}


@pytest.fixture
async def router():
    config = BackendConfig(command=(sys.executable, str(FAKE)), start_timeout=30)
    pool = Pool(config)
    tools = await probe_tools(config)
    yield pool, tools
    await pool.close()


def connect(router):
    pool, tools = router
    return create_connected_server_and_client_session(build_server(pool, tools))


def _text(result) -> str:
    return "\n".join(c.text for c in result.content if getattr(c, "type", "") == "text")


async def test_each_session_edits_only_its_own_worktree(router, repo):
    async with connect(router) as a, connect(router) as b:
        await a.call_tool("activate_project", {"project": str(repo["a"])})
        await b.call_tool("activate_project", {"project": str(repo["b"])})
        await a.call_tool("replace_content", {"relative_path": "f.txt", "repl": "from-a"})
        await b.call_tool("replace_content", {"relative_path": "f.txt", "repl": "from-b"})
        assert (repo["a"] / "f.txt").read_text() == "from-a"
        assert (repo["b"] / "f.txt").read_text() == "from-b"
        assert not (repo["primary"] / "f.txt").exists()


async def test_edit_refused_while_unbound(router, repo):
    async with connect(router) as session:
        result = await session.call_tool("replace_content", {"relative_path": "f.txt", "repl": "x"})
        assert result.isError
        assert UNBOUND in _text(result)


async def test_edit_refused_on_primary_checkout(router, repo):
    async with connect(router) as session:
        activated = await session.call_tool("activate_project", {"project": str(repo["primary"])})
        assert "primary checkout" in _text(activated)
        result = await session.call_tool("replace_content", {"relative_path": "f.txt", "repl": "x"})
        assert result.isError
        assert not (repo["primary"] / "f.txt").exists()


async def test_reads_allowed_on_primary_checkout(router, repo):
    async with connect(router) as session:
        await session.call_tool("activate_project", {"project": str(repo["primary"])})
        result = await session.call_tool("find_symbol", {"name_path_pattern": "x"})
        assert not result.isError
        assert f"root={repo['primary'].resolve()}" in _text(result)


async def test_relative_activation_refused(router, repo):
    async with connect(router) as session:
        result = await session.call_tool("activate_project", {"project": "wt-a"})
        assert result.isError
        assert "absolute path" in _text(result)


async def test_unannotated_tool_counts_as_edit(router):
    _, tools = router
    edits = edit_tools(tools)
    assert "mystery" in edits
    assert "replace_content" in edits
    assert "find_symbol" not in edits


async def test_slow_backend_does_not_block_another(router, repo):
    async with connect(router) as a, connect(router) as b:
        await a.call_tool("activate_project", {"project": str(repo["a"])})
        await b.call_tool("activate_project", {"project": str(repo["b"])})
        slow = asyncio.create_task(a.call_tool("slow", {"seconds": 3}))
        await asyncio.sleep(0.3)
        started = time.monotonic()
        await b.call_tool("find_symbol", {})
        assert time.monotonic() - started < 1.5
        assert not slow.done()
        await slow


async def test_release_stops_backend_and_next_call_restarts_it(router, repo):
    pool, _ = router
    async with connect(router) as session:
        await session.call_tool("activate_project", {"project": str(repo["a"])})
        root = repo["a"].resolve()
        assert await pool.release(root)
        assert pool.snapshot() == []
        result = await session.call_tool("find_symbol", {})
        assert not result.isError
        assert [r["root"] for r in pool.snapshot()] == [str(root)]


async def test_removed_worktree_unbinds_the_session(router, repo):
    async with connect(router) as session:
        await session.call_tool("activate_project", {"project": str(repo["a"])})
        _git(repo["primary"], "worktree", "remove", "--force", str(repo["a"]))
        result = await session.call_tool("find_symbol", {})
        assert result.isError
        assert UNBOUND in _text(result)


async def test_untracked_serena_dir_is_excluded(router, repo):
    async with connect(router) as session:
        await session.call_tool("activate_project", {"project": str(repo["a"])})
        exclude = repo["primary"] / ".git" / "info" / "exclude"
        assert ".serena/" in exclude.read_text().splitlines()


def test_app_bounds_idle_sessions():
    app = build_app(Pool(BackendConfig(command=(sys.executable, str(FAKE)))), [])
    assert app.state.session_manager.session_idle_timeout == SESSION_IDLE_SECONDS
