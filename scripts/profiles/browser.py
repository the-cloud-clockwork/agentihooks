from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mcp import ClientSession, types
    from mcp.client.stdio import StdioServerParameters
    from mcp.server.lowlevel import Server

from hooks.context.profile_chain import PACKAGE_PREFIX
from scripts.targets._common import _install_module

ROLES = frozenset(("engineer", "cicd", "planner", "master", "qa", "documentation"))
NAME = "playwright-cmd"
EXTENSION = "playwright-ext-"


def enabled(chain: list[str]) -> bool:
    return any(name.removeprefix(PACKAGE_PREFIX) in ROLES for name in chain)


def proxy_enabled(chain: list[str]) -> bool:
    return enabled(chain) and any(
        name.removeprefix(PACKAGE_PREFIX) in {"master", "qa", "documentation", "frontend"} for name in chain
    )


def spec() -> dict:
    install = _install_module()
    return {"command": str(install._detect_venv() or sys.executable), "args": ["-I", "-m", __name__]}


def configure(servers: dict, chain: list[str]) -> dict:
    if not enabled(chain):
        return servers
    return {
        **{
            name: server
            for name, server in servers.items()
            if "playwright" not in name.lower() or name.startswith(EXTENSION)
        },
        **({NAME: spec()} if proxy_enabled(chain) else {}),
    }


def output_folder() -> Path:
    from scripts.swarm_ledger.ledger_workspace import folder

    swarm, task = os.environ.get("AGENTIHOOKS_SWARM"), os.environ.get("AGENTIHOOKS_SWARM_TASK")
    out = folder(swarm, task or "master") if swarm else Path.home() / ".agentihooks" / "browser"
    out.mkdir(parents=True, exist_ok=True)
    return out.resolve()


def parameters(out: Path, executable: str) -> StdioServerParameters:
    from mcp.client.stdio import StdioServerParameters

    env = {
        key: value
        for key, value in os.environ.items()
        if key not in ("DISPLAY", "WAYLAND_DISPLAY") and not key.startswith("PLAYWRIGHT_MCP_")
    }
    return StdioServerParameters(
        command="playwright-mcp",
        args=[
            "--headless",
            "--isolated",
            "--executable-path",
            executable,
            "--viewport-size",
            "1920x1080",
            "--output-dir",
            str(out),
        ],
        cwd=str(out),
        env=env,
    )


async def call(session: ClientSession, out: Path, name: str, arguments: dict) -> types.CallToolResult:
    args = dict(arguments)
    if args.get("filename"):
        args["filename"] = str(out / Path(args["filename"]).name)
    return await session.call_tool(name, args)


def proxy(session: ClientSession | Upstream, out: Path) -> Server:
    from mcp.server.lowlevel import Server

    server = Server("swarm-browser")

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return (await session.list_tools()).tools

    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> types.CallToolResult:
        return await call(session, out, name, arguments)

    return server


def fingerprint() -> str | None:
    found = shutil.which("playwright-mcp")
    if not found:
        return None
    cli = Path(found).resolve()
    return f"{cli} {cli.stat().st_mtime_ns}"


def tools_file() -> Path:
    return Path.home() / ".agentihooks" / "browser" / "tools.json"


def cached_tools(key: str | None) -> list[types.Tool] | None:
    from mcp import types

    if key is None:
        return None
    try:
        saved = json.loads(tools_file().read_text())
        if saved["key"] != key:
            return None
        return [types.Tool.model_validate(tool) for tool in saved["tools"]]
    except (OSError, ValueError, KeyError, TypeError):
        return None


def save_tools(tools: list[types.Tool], key: str | None) -> None:
    if key is None:
        return
    path = tools_file()
    path.parent.mkdir(exist_ok=True)
    dumped = [tool.model_dump(by_alias=True) for tool in tools]
    path.write_text(json.dumps({"key": key, "tools": dumped}))


class Upstream:
    def __init__(self, out: Path) -> None:
        import anyio

        self.out = out
        self.wanted = anyio.Event()
        self.ready = anyio.Event()

    async def run(self) -> None:
        await self.wanted.wait()
        import anyio
        from mcp import ClientSession, types
        from mcp.client.stdio import stdio_client
        from playwright.async_api import async_playwright

        async def roots(context) -> types.ListRootsResult:
            return types.ListRootsResult(roots=[types.Root(uri=self.out.as_uri())])

        async with async_playwright() as playwright:
            params = parameters(self.out, playwright.chromium.executable_path)
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write, list_roots_callback=roots) as session:
                await session.initialize()
                self.session = session
                self.ready.set()
                await anyio.sleep_forever()

    async def started(self) -> ClientSession:
        self.wanted.set()
        await self.ready.wait()
        return self.session

    async def list_tools(self) -> types.ListToolsResult:
        from mcp import types

        key = fingerprint()
        tools = cached_tools(key)
        if tools is not None:
            return types.ListToolsResult(tools=tools)
        result = await (await self.started()).list_tools()
        save_tools(result.tools, key)
        return result

    async def call_tool(self, name: str, arguments: dict) -> types.CallToolResult:
        return await (await self.started()).call_tool(name, arguments)


async def serve() -> None:
    import anyio
    from mcp.server.stdio import stdio_server

    out = output_folder()
    upstream = Upstream(out)
    server = proxy(upstream, out)
    async with anyio.create_task_group() as group:
        group.start_soon(upstream.run)
        async with stdio_server() as (incoming, outgoing):
            await server.run(incoming, outgoing, server.create_initialization_options())
        group.cancel_scope.cancel()


if __name__ == "__main__":
    asyncio.run(serve())
