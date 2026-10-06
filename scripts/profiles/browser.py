from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from mcp import ClientSession, types
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from playwright.async_api import async_playwright

from hooks.context.profile_chain import PACKAGE_PREFIX
from scripts.targets._common import _install_module

ROLES = frozenset(("engineer", "cicd", "planner", "master", "qa"))
NAME = "playwright-cmd"


def enabled(chain: list[str]) -> bool:
    return any(name.removeprefix(PACKAGE_PREFIX) in ROLES for name in chain)


def spec() -> dict:
    install = _install_module()
    return {"command": str(install._detect_venv() or sys.executable), "args": ["-I", "-m", __name__]}


def configure(servers: dict, chain: list[str]) -> dict:
    if not enabled(chain):
        return servers
    return {
        **{name: server for name, server in servers.items() if "playwright" not in name.lower()},
        NAME: spec(),
    }


def output_folder() -> Path:
    from scripts.swarm_ledger.ledger_workspace import folder

    swarm, task = os.environ.get("AGENTIHOOKS_SWARM"), os.environ.get("AGENTIHOOKS_SWARM_TASK")
    out = folder(swarm, task or "master") if swarm else Path.home() / ".agentihooks" / "browser"
    out.mkdir(parents=True, exist_ok=True)
    return out.resolve()


def parameters(out: Path, executable: str) -> StdioServerParameters:
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


def proxy(session: ClientSession, out: Path) -> Server:
    server = Server("swarm-browser")

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return (await session.list_tools()).tools

    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> types.CallToolResult:
        return await call(session, out, name, arguments)

    return server


async def serve() -> None:
    out = output_folder()

    async def roots(context) -> types.ListRootsResult:
        return types.ListRootsResult(roots=[types.Root(uri=out.as_uri())])

    async with async_playwright() as playwright:
        params = parameters(out, playwright.chromium.executable_path)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write, list_roots_callback=roots) as session:
            await session.initialize()
            server = proxy(session, out)
            async with stdio_server() as (incoming, outgoing):
                await server.run(incoming, outgoing, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(serve())
