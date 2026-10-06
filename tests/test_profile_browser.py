from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from mcp import types

from scripts.profiles import browser


def test_task_browser_uses_the_task_folder_without_a_display(monkeypatch):
    monkeypatch.setenv("AGENTIHOOKS_SWARM", "proof-swarm")
    monkeypatch.setenv("AGENTIHOOKS_SWARM_TASK", "hp1")
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.setenv("PLAYWRIGHT_MCP_CDP_ENDPOINT", "http://localhost:9222")
    folder = browser.output_folder()
    assert folder == Path.home() / ".agentihooks/swarm/proof-swarm/tasks/hp1"
    assert folder.is_dir()
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
async def test_screenshot_names_stay_in_the_task_folder(tmp_path, filename):
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


@pytest.mark.asyncio
async def test_proxy_exposes_upstream_tools_and_preserves_failures(tmp_path):
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
async def test_server_runs_the_task_browser_over_mcp(monkeypatch):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    import anyio
    from mcp import ClientSession

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
    to_server, server_read = anyio.create_memory_object_stream(10)
    to_client, client_read = anyio.create_memory_object_stream(10)

    @asynccontextmanager
    async def playwright():
        yield SimpleNamespace(chromium=SimpleNamespace(executable_path="/browser/chromium"))

    @asynccontextmanager
    async def transport(params):
        launch["params"] = params
        yield None, None

    def session(*args, **kwargs):
        launch["roots"] = kwargs["list_roots_callback"]
        return upstream

    @asynccontextmanager
    async def stdio():
        yield server_read, to_client

    monkeypatch.setattr(browser, "async_playwright", playwright)
    monkeypatch.setattr(browser, "stdio_client", transport)
    monkeypatch.setattr(browser, "ClientSession", session)
    monkeypatch.setattr(browser, "stdio_server", stdio)
    async with anyio.create_task_group() as group:
        group.start_soon(browser.serve)
        async with ClientSession(client_read, to_server) as client:
            await client.initialize()
            assert (await client.list_tools()).tools == [tool]
            assert await client.call_tool("browser_take_screenshot", {"filename": "/repo/proof.png"}) == result
        group.cancel_scope.cancel()
    assert launch["params"] == browser.parameters(out, "/browser/chromium")
    assert (await launch["roots"](None)).roots == [types.Root(uri=out.as_uri())]
    upstream.initialize.assert_awaited_once()
    upstream.call_tool.assert_awaited_once_with("browser_take_screenshot", {"filename": str(out / "proof.png")})
