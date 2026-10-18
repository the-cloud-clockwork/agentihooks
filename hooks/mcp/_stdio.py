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
            f"[agentihooks] WARNING: ignoring unknown MCP categories {unknown}; valid categories: {sorted(CATALOG)}",
            file=sys.stderr,
        )
    allowed_raw = os.getenv("ALLOWED_TOOLS", "").strip()
    allowed = {name.strip().lower() for name in allowed_raw.split(",") if name.strip()}
    tools = {
        tool["name"]: tool
        for category in categories
        for tool in CATALOG.get(category, [])
        if not allowed_raw or tool["name"].lower() in allowed
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


async def _serve(history, stdin, stdout, initialize_id) -> None:
    import anyio
    from mcp.server.stdio import stdio_server

    from hooks.mcp import build_server

    server = build_server()

    async def incoming():
        for message in history:
            yield message
        async for line in anyio.wrap_file(stdin):
            yield line

    output = _Output(anyio.wrap_file(stdout), initialize_id)
    async with stdio_server(stdin=incoming(), stdout=output) as (read, write):
        await server._mcp_server.run(read, write, server._mcp_server.create_initialization_options())


def _method(message):
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        return None
    params = message.get("params", {})
    if not isinstance(params, dict):
        return None
    method = message.get("method")
    if method == "notifications/initialized" and "id" not in message:
        return method
    request_id = message.get("id")
    if isinstance(request_id, bool) or not isinstance(request_id, (str, int)):
        return None
    if method == "initialize":
        client = params.get("clientInfo", {})
        if not isinstance(client, dict):
            return None
        fields = (params.get("protocolVersion"), client.get("name"), client.get("version"))
        if not all(isinstance(value, str) for value in fields) or not isinstance(params.get("capabilities"), dict):
            return None
    return method


def run() -> None:
    history = []
    initialize_id = None
    tools = list_tools()
    for line in sys.stdin:
        try:
            message = json.loads(line)
        except ValueError:
            message = None
        method = _method(message)
        if method == "initialize":
            history = [line]
            initialize_id = message["id"]
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
        elif method in {"resources/list", "resources/templates/list", "prompts/list"} and initialize_id is not None:
            key = {
                "resources/list": "resources",
                "resources/templates/list": "resourceTemplates",
                "prompts/list": "prompts",
            }[method]
            result = {key: []}
        elif method == "tools/list" and initialize_id is not None:
            result = {"tools": tools}
        else:
            import anyio

            anyio.run(_serve, history + [line], sys.stdin, sys.stdout, initialize_id)
            return
        print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
