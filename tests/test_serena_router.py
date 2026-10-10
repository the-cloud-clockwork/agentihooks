from __future__ import annotations

import ast
import asyncio
import subprocess
import sys
from pathlib import Path

import pytest

# The MCP SDK loads inside the tests: every worker of a shard collects this file, one worker runs every file that loads it.
pytestmark = pytest.mark.xdist_group("mcp-sdk")

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
    from hooks.serena_router.pool import BackendConfig, Pool, probe_tools

    config = BackendConfig(command=(sys.executable, str(FAKE)), start_timeout=30)
    pool = Pool(config)
    tools = await probe_tools(config)
    yield pool, tools
    await pool.close()


def connect(router):
    from mcp.shared.memory import create_connected_server_and_client_session

    from hooks.serena_router.server import build_server

    pool, tools = router
    return create_connected_server_and_client_session(build_server(pool, tools))


def _text(result) -> str:
    return "\n".join(c.text for c in result.content if getattr(c, "type", "") == "text")


def test_backend_starts_when_the_sdk_was_imported_under_captured_stderr():
    script = (
        "import asyncio, io, sys\n"
        "sys.stderr = io.StringIO()\n"
        "from hooks.serena_router.pool import BackendConfig, probe_tools\n"
        "sys.stderr = sys.__stderr__\n"
        f"config = BackendConfig(command=(sys.executable, {str(FAKE)!r}), start_timeout=30)\n"
        "assert asyncio.run(probe_tools(config))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=Path(__file__).parents[1], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stderr


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
    from hooks.serena_router.server import UNBOUND

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
    from hooks.serena_router.server import edit_tools

    _, tools = router
    edits = edit_tools(tools)
    assert "mystery" in edits
    assert "replace_content" in edits
    assert "find_symbol" not in edits


async def _appears(path: Path) -> None:
    while not path.exists():
        await asyncio.sleep(0.005)


async def test_slow_backend_does_not_block_another(router, repo, tmp_path):
    started, release = tmp_path / "started", tmp_path / "release"
    async with connect(router) as a, connect(router) as b:
        await a.call_tool("activate_project", {"project": str(repo["a"])})
        await b.call_tool("activate_project", {"project": str(repo["b"])})
        slow = asyncio.create_task(a.call_tool("slow", {"started": str(started), "release": str(release)}))
        try:
            await asyncio.wait_for(_appears(started), timeout=5)
            await asyncio.wait_for(b.call_tool("find_symbol", {}), timeout=5)
            assert not slow.done()
        finally:
            release.touch()
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
    from hooks.serena_router.server import UNBOUND

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


async def test_worktree_inherits_the_primary_checkout_serena_config(router, repo):
    config = repo["primary"] / ".serena" / "project.yml"
    config.parent.mkdir()
    config.write_text('language_servers: ["python"]\n')
    async with connect(router) as session:
        await session.call_tool("activate_project", {"project": str(repo["a"])})
    assert (repo["a"] / ".serena" / "project.yml").read_text() == 'language_servers: ["python"]\n'


def test_app_bounds_idle_sessions():
    from hooks.serena_router.app import SESSION_IDLE_SECONDS, build_app
    from hooks.serena_router.pool import BackendConfig, Pool

    app = build_app(Pool(BackendConfig(command=(sys.executable, str(FAKE)))), [])
    assert app.state.session_manager.session_idle_timeout == SESSION_IDLE_SECONDS


OPT_IN = {"read_file", "search_for_pattern"}


async def _listed_over_http(router, path: str) -> set[str]:
    import json

    import httpx

    from hooks.serena_router.app import build_app

    app = build_app(*router)
    headers = {"Accept": "application/json, text/event-stream"}
    init = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}},
    }
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://router") as client,
    ):
        started = await client.post(path, json=init, headers=headers)
        headers["mcp-session-id"] = started.headers["mcp-session-id"]
        await client.post(path, json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=headers)
        listed = await client.post(path, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, headers=headers)
    data = [line.removeprefix("data:") for line in listed.text.splitlines() if line.startswith("data:")]
    return {tool["name"] for tool in json.loads(data[-1])["result"]["tools"]}


async def test_default_listing_hides_the_opt_in_tools(router):
    async with connect(router) as session:
        names = {tool.name for tool in (await session.list_tools()).tools}
    assert "find_symbol" in names
    assert not names & OPT_IN


async def test_mcp_endpoint_hides_the_opt_in_tools_without_a_query(router):
    names = await _listed_over_http(router, "/mcp")
    assert "find_symbol" in names
    assert not names & OPT_IN


async def test_mcp_endpoint_lists_the_opt_in_tools_its_query_names(router):
    assert await _listed_over_http(router, "/mcp?tools=read_file,search_for_pattern") >= OPT_IN | {"find_symbol"}
    assert OPT_IN - await _listed_over_http(router, "/mcp?tools=read_file") == {"search_for_pattern"}


def test_listed_ignores_names_outside_the_opt_in_set():
    from mcp.types import Tool

    from hooks.serena_router.server import listed

    tools = [Tool(name=name, inputSchema={"type": "object"}) for name in ("find_symbol", "read_file", "mystery")]
    assert [t.name for t in listed(tools, [])] == ["find_symbol", "mystery"]
    assert [t.name for t in listed(tools, ["mystery,", "read_file"])] == ["find_symbol", "read_file", "mystery"]


def test_fake_backend_imports_only_the_standard_library():
    tree = ast.parse(FAKE.read_text())
    modules = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    modules |= {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    assert {m.split(".")[0] for m in modules} <= sys.stdlib_module_names
