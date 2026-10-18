import json
import os
import sys
from importlib.metadata import version

from hooks.mcp import _resolve_categories
from hooks.mcp._catalog import CAPABILITIES, CATALOG, PROTOCOL_VERSIONS


def list_tools() -> list[dict]:
    categories = _resolve_categories()
    unknown = [category for category in categories if category not in CATALOG]
    if unknown:
        print(
            f"[agentihooks] WARNING: ignoring unknown MCP categories {unknown}; "
            f"valid categories: {sorted(CATALOG)}",
            file=sys.stderr,
        )
    allowed = {name.strip().lower() for name in os.getenv("ALLOWED_TOOLS", "").split(",") if name.strip()}
    tools = {
        tool["name"]: tool
        for category in categories
        for tool in CATALOG.get(category, [])
        if not allowed or tool["name"].lower() in allowed
    }
    if not tools:
        print(
            "[agentihooks] WARNING: MCP server started with ZERO tools "
            f"(requested categories: {categories}). Check MCP_CATEGORIES.",
            file=sys.stderr,
        )
    return list(tools.values())


class _Output:
    def __init__(self, stream, initialize_id):
        self.stream = stream
        self.initialize_id = initialize_id
        self.pending = initialize_id is not None

    async def write(self, data: str) -> None:
        message = json.loads(data)
        if self.pending and message.get("id") == self.initialize_id:
            self.pending = False
            return
        await self.stream.write(data)

    async def flush(self) -> None:
        await self.stream.flush()


async def _serve(history, stdin, stdout) -> None:
    import anyio
    from mcp.server.stdio import stdio_server

    from hooks.mcp import build_server

    server = build_server()

    async def incoming():
        for message in history:
            yield message
        async for line in anyio.wrap_file(stdin):
            yield line

    first = json.loads(history[0]) if history and history[0].lstrip().startswith("{") else {}
    initialize_id = first.get("id") if first.get("method") == "initialize" else None
    output = _Output(anyio.wrap_file(stdout), initialize_id)
    async with stdio_server(stdin=incoming(), stdout=output) as (read, write):
        await server._mcp_server.run(read, write, server._mcp_server.create_initialization_options())


def run() -> None:
    history = []
    tools = list_tools()
    for line in sys.stdin:
        try:
            message = json.loads(line)
        except ValueError:
            import anyio

            anyio.run(_serve, history + [line], sys.stdin, sys.stdout)
            return
        method = message.get("method")
        if method == "initialize":
            history = [line]
            requested = message["params"]["protocolVersion"]
            result = {
                "protocolVersion": requested if requested in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[-1],
                "capabilities": CAPABILITIES,
                "serverInfo": {"name": "agentihooks", "version": version("mcp")},
            }
        elif method == "notifications/initialized":
            history.append(line)
            continue
        elif method == "ping":
            result = {}
        elif method == "tools/list" and history:
            result = {"tools": tools}
        else:
            import anyio

            anyio.run(_serve, history + [line], sys.stdin, sys.stdout)
            return
        print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
