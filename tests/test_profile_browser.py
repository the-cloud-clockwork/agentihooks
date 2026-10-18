import os
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from scripts.profiles import browser

pytestmark = pytest.mark.xdist_group("mcp-sdk")


@pytest.fixture
def types():
    from mcp import types

    return types


def test_task_browser_uses_the_task_folder_without_a_display(monkeypatch):
    monkeypatch.setenv("AGENTIHOOKS_SWARM", "proof-swarm")
    monkeypatch.setenv("AGENTIHOOKS_SWARM_TASK", "hp1")
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.setenv("PLAYWRIGHT_MCP_CDP_ENDPOINT", "http://localhost:9222")
    folder = browser.output_folder()
    assert folder == Path.home() / ".agentihooks/swarm/proof-swarm/tasks/hp1"
    assert folder.is_dir()
    assert browser.output_folder() == folder
    params = browser.parameters(folder, "/browser/chromium")
    assert params.cwd == str(folder)
    assert params.command == "playwright-mcp"
    assert params.args == [
        "--headless",
        "--isolated",
        "--executable-path",
        "/browser/chromium",
        "--viewport-size",
        "1920x1080",
        "--output-dir",
        str(folder),
    ]
    assert not {"DISPLAY", "WAYLAND_DISPLAY", "PLAYWRIGHT_MCP_CDP_ENDPOINT"} & params.env.keys()


@pytest.mark.asyncio
@pytest.mark.parametrize("filename", ["proof.png", "../../proof.png", "/repo/proof.png"])
async def test_screenshot_names_stay_in_the_task_folder(tmp_path, filename, types):
    session = AsyncMock()
    result = types.CallToolResult(content=[types.TextContent(type="text", text="saved")])
    session.call_tool.return_value = result
    assert await browser.call(session, tmp_path, "browser_take_screenshot", {"filename": filename}) == result
    session.call_tool.assert_awaited_once_with("browser_take_screenshot", {"filename": str(tmp_path / "proof.png")})


@pytest.mark.asyncio
async def test_browser_forwards_unnamed_screenshots_and_other_tools(tmp_path):
    session = AsyncMock()
    await browser.call(session, tmp_path, "browser_take_screenshot", {})
    await browser.call(session, tmp_path, "browser_navigate", {"url": "http://localhost:8765"})
    assert session.call_tool.await_args_list[0].args == ("browser_take_screenshot", {})
    assert session.call_tool.await_args_list[1].args == ("browser_navigate", {"url": "http://localhost:8765"})


@pytest.mark.parametrize(
    "swarm, task, suffix",
    [
        ("proof-swarm", "", ".agentihooks/swarm/proof-swarm/tasks/master"),
        ("", "", ".agentihooks/browser"),
    ],
)
def test_browser_without_a_task_stays_outside_checkouts(monkeypatch, swarm, task, suffix):
    monkeypatch.setenv("AGENTIHOOKS_SWARM", swarm)
    monkeypatch.setenv("AGENTIHOOKS_SWARM_TASK", task)
    assert browser.output_folder() == Path.home() / suffix


def test_missing_swarm_environment_uses_the_standalone_folder(monkeypatch):
    monkeypatch.delenv("AGENTIHOOKS_SWARM", raising=False)
    monkeypatch.delenv("AGENTIHOOKS_SWARM_TASK", raising=False)
    assert browser.output_folder() == Path.home() / ".agentihooks/browser"
    monkeypatch.setenv("AGENTIHOOKS_SWARM", "proof-swarm")
    assert browser.output_folder() == Path.home() / ".agentihooks/swarm/proof-swarm/tasks/master"


@pytest.mark.asyncio
async def test_proxy_exposes_upstream_tools_and_preserves_failures(tmp_path, types):
    session = AsyncMock()
    tool = types.Tool(name="browser_take_screenshot", inputSchema={"type": "object"})
    session.list_tools.return_value = types.ListToolsResult(tools=[tool])
    failure = types.CallToolResult(isError=True, content=[types.TextContent(type="text", text="browser failed")])
    session.call_tool.return_value = failure
    server = browser.proxy(session, tmp_path)
    listed = await server.request_handlers[types.ListToolsRequest](types.ListToolsRequest(method="tools/list"))
    assert listed.root.tools == [tool]
    called = await server.request_handlers[types.CallToolRequest](
        types.CallToolRequest(
            method="tools/call",
            params=types.CallToolRequestParams(
                name="browser_take_screenshot",
                arguments={"filename": "/repo/proof.png"},
            ),
        )
    )
    assert called.root == failure
    session.call_tool.assert_awaited_once_with("browser_take_screenshot", {"filename": str(tmp_path / "proof.png")})


@pytest.mark.asyncio
async def test_server_runs_the_task_browser_over_mcp(monkeypatch, types):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    import anyio
    from mcp import ClientSession
    from mcp.client import stdio as client_stdio

    monkeypatch.setenv("AGENTIHOOKS_SWARM", "proof-swarm")
    monkeypatch.setenv("AGENTIHOOKS_SWARM_TASK", "hp1")
    out = Path.home() / ".agentihooks/swarm/proof-swarm/tasks/hp1"
    upstream = AsyncMock()
    upstream.__aenter__.return_value = upstream
    tool = types.Tool(name="browser_take_screenshot", inputSchema={"type": "object"})
    upstream.list_tools.return_value = types.ListToolsResult(tools=[tool])
    result = types.CallToolResult(content=[types.TextContent(type="text", text="saved in task")])
    upstream.call_tool.return_value = result
    launch = {}
    upstream_read, upstream_write = object(), object()
    to_server, server_read = anyio.create_memory_object_stream(10)
    to_client, client_read = anyio.create_memory_object_stream(10)

    @asynccontextmanager
    async def playwright():
        yield SimpleNamespace(chromium=SimpleNamespace(executable_path="/browser/chromium"))

    @asynccontextmanager
    async def transport(params):
        launch["params"] = params
        yield upstream_read, upstream_write

    def session(read, write, **kwargs):
        assert read is upstream_read and write is upstream_write
        launch["roots"] = kwargs["list_roots_callback"]
        return upstream

    @asynccontextmanager
    async def stdio():
        yield server_read, to_client

    monkeypatch.setattr("playwright.async_api.async_playwright", playwright)
    monkeypatch.setattr(client_stdio, "stdio_client", transport)
    monkeypatch.setattr("mcp.ClientSession", session)
    monkeypatch.setattr("mcp.server.stdio.stdio_server", stdio)
    async with anyio.create_task_group() as group:
        group.start_soon(browser.serve)
        async with ClientSession(client_read, to_server) as client:
            assert (await client.initialize()).serverInfo.name == "swarm-browser"
            assert (await client.list_tools()).tools == [tool]
            assert await client.call_tool("browser_take_screenshot", {"filename": "/repo/proof.png"}) == result
        group.cancel_scope.cancel()
    assert launch["params"] == browser.parameters(out, "/browser/chromium")
    assert (await launch["roots"](None)).roots == [types.Root(uri=out.as_uri())]
    upstream.initialize.assert_awaited_once()
    upstream.call_tool.assert_awaited_once_with("browser_take_screenshot", {"filename": str(out / "proof.png")})


def _upstream(types):
    upstream = AsyncMock()
    upstream.__aenter__.return_value = upstream
    tool = types.Tool.model_validate(
        {"name": "browser_navigate", "inputSchema": {"type": "object"}, "_meta": {"source": "playwright"}}
    )
    upstream.list_tools.return_value = types.ListToolsResult(tools=[tool])
    upstream.call_tool.return_value = types.CallToolResult(content=[types.TextContent(type="text", text="ok")])
    return upstream, tool


async def _session_against(monkeypatch, upstream, actions):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    import anyio
    from mcp import ClientSession
    from mcp.client import stdio as client_stdio

    launches = []
    to_server, server_read = anyio.create_memory_object_stream(10)
    to_client, client_read = anyio.create_memory_object_stream(10)

    @asynccontextmanager
    async def playwright():
        launches.append("playwright")
        yield SimpleNamespace(chromium=SimpleNamespace(executable_path="/browser/chromium"))

    @asynccontextmanager
    async def transport(params):
        launches.append("playwright-mcp")
        yield object(), object()

    @asynccontextmanager
    async def stdio():
        yield server_read, to_client

    monkeypatch.setattr("playwright.async_api.async_playwright", playwright)
    monkeypatch.setattr(client_stdio, "stdio_client", transport)
    monkeypatch.setattr("mcp.ClientSession", lambda read, write, **kwargs: upstream)
    monkeypatch.setattr("mcp.server.stdio.stdio_server", stdio)
    async with anyio.create_task_group() as group:
        group.start_soon(browser.serve)
        async with ClientSession(client_read, to_server) as client:
            await client.initialize()
            results = [await action(client) for action in actions]
        group.cancel_scope.cancel()
    return launches, results


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [1, 0])
async def test_a_session_that_only_lists_cached_tools_never_launches_the_browser(monkeypatch, types, count):
    monkeypatch.setattr(browser, "fingerprint", lambda: "playwright-mcp 1")
    upstream, tool = _upstream(types)
    browser.save_tools([tool] * count, "playwright-mcp 1")
    launches, results = await _session_against(monkeypatch, upstream, [lambda client: client.list_tools()])
    assert results[0].tools == [tool] * count
    assert launches == []
    upstream.initialize.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_list_without_a_matching_cache_launches_once_and_caches_the_tools(monkeypatch, types):
    upstream, tool = _upstream(types)
    monkeypatch.setattr(browser, "fingerprint", lambda: "playwright-mcp 1")
    browser.save_tools([types.Tool(name="stale", inputSchema={"type": "object"})], "playwright-mcp 1")
    monkeypatch.setattr(browser, "fingerprint", lambda: "playwright-mcp 2")
    listing = [lambda client: client.list_tools()] * 2
    launches, results = await _session_against(monkeypatch, upstream, listing)
    assert [r.tools for r in results] == [[tool], [tool]]
    assert launches == ["playwright", "playwright-mcp"]
    upstream.list_tools.assert_awaited_once()
    assert browser.cached_tools("playwright-mcp 2") == [tool]
    upstream, _ = _upstream(types)
    launches, results = await _session_against(monkeypatch, upstream, listing[:1])
    assert results[0].tools == [tool]
    assert launches == []


@pytest.mark.asyncio
@pytest.mark.parametrize("saved", [None, '{"key": null, "tools": []}'])
async def test_without_playwright_mcp_on_the_path_nothing_is_cached(monkeypatch, types, saved):
    monkeypatch.setattr("shutil.which", lambda name: None)
    assert browser.fingerprint() is None
    if saved is not None:
        browser.tools_file().parent.mkdir(exist_ok=True)
        browser.tools_file().write_text(saved)
    upstream, tool = _upstream(types)
    launches, results = await _session_against(monkeypatch, upstream, [lambda client: client.list_tools()])
    assert results[0].tools == [tool]
    assert launches == ["playwright", "playwright-mcp"]
    assert (browser.tools_file().read_text() if saved else browser.tools_file().exists()) == (saved or False)


def test_the_cache_key_follows_the_installed_playwright_mcp(monkeypatch, tmp_path):
    cli = tmp_path / "cli.js"
    cli.write_text("one")
    link = tmp_path / "playwright-mcp"
    link.symlink_to(cli)
    monkeypatch.setattr("shutil.which", lambda name: str(link) if name == "playwright-mcp" else None)
    first = browser.fingerprint()
    assert str(cli) in first
    os.utime(cli, ns=(1, 1))
    assert browser.fingerprint() != first


@pytest.mark.parametrize(
    "content",
    [
        None,
        "{not json",
        "null",
        "[]",
        '{"tools": []}',
        '{"key": "playwright-mcp 1"}',
        '{"key": "playwright-mcp 1", "tools": [{"name": 5}]}',
    ],
)
def test_a_missing_or_unreadable_cache_is_no_cache(content):
    assert browser.tools_file() == Path.home() / ".agentihooks/browser/tools.json"
    if content is not None:
        browser.tools_file().parent.mkdir(exist_ok=True)
        browser.tools_file().write_text(content)
    assert browser.cached_tools("playwright-mcp 1") is None


@pytest.mark.asyncio
async def test_the_first_tool_call_launches_the_browser_once_and_keeps_the_task_folder(monkeypatch, types):
    monkeypatch.setenv("AGENTIHOOKS_SWARM", "proof-swarm")
    monkeypatch.setenv("AGENTIHOOKS_SWARM_TASK", "hp1")
    monkeypatch.setattr(browser, "fingerprint", lambda: "playwright-mcp 1")
    upstream, tool = _upstream(types)
    browser.save_tools([tool], "playwright-mcp 1")
    out = Path.home() / ".agentihooks/swarm/proof-swarm/tasks/hp1"
    calls = [
        lambda client: client.list_tools(),
        lambda client: client.call_tool("browser_take_screenshot", {"filename": "../proof.png"}),
        lambda client: client.call_tool("browser_navigate", {"url": "http://localhost:8765"}),
    ]
    launches, results = await _session_against(monkeypatch, upstream, calls)
    assert launches == ["playwright", "playwright-mcp"]
    upstream.initialize.assert_awaited_once()
    upstream.list_tools.assert_not_awaited()
    assert [c.args for c in upstream.call_tool.await_args_list] == [
        ("browser_take_screenshot", {"filename": str(out / "proof.png")}),
        ("browser_navigate", {"url": "http://localhost:8765"}),
    ]
