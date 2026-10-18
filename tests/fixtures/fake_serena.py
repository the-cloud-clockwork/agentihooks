"""Stand-in for `serena start-mcp-server`: same CLI shape, tools that touch only --project.

Speaks MCP JSON-RPC over stdio with the standard library alone, so a spawn costs no SDK import.
"""

import json
import sys
import time
from pathlib import Path

project = Path(sys.argv[sys.argv.index("--project") + 1]) if "--project" in sys.argv else None


def find_symbol(name_path_pattern: str = "") -> str:
    return f"root={project}"


def replace_content(relative_path: str, repl: str) -> str:
    (project / relative_path).write_text(repl)
    return "OK"


def slow(started: str, release: str) -> str:
    Path(started).touch()
    while not Path(release).exists():
        time.sleep(0.005)
    return "released"


def mystery() -> str:
    return "unannotated"


def activate_project(project: str) -> str:
    return "the router must never forward this"


TOOLS = {
    "find_symbol": (find_symbol, {"name_path_pattern": "string"}, True),
    "replace_content": (replace_content, {"relative_path": "string", "repl": "string"}, False),
    "slow": (slow, {"started": "string", "release": "string"}, True),
    "mystery": (mystery, {}, None),
    "activate_project": (activate_project, {"project": "string"}, True),
    "read_file": (find_symbol, {"relative_path": "string"}, True),
    "search_for_pattern": (find_symbol, {"substring_pattern": "string"}, True),
}


def _tool(name: str, properties: dict, read_only: bool | None) -> dict:
    tool = {
        "name": name,
        "inputSchema": {"type": "object", "properties": {k: {"type": v} for k, v in properties.items()}},
    }
    if read_only is not None:
        tool["annotations"] = {"readOnlyHint": read_only}
    return tool


def handle(method: str, params: dict) -> dict:
    if method == "initialize":
        return {
            "protocolVersion": params["protocolVersion"],
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "fake-serena", "version": "0"},
        }
    if method == "tools/list":
        return {"tools": [_tool(name, props, ro) for name, (_, props, ro) in TOOLS.items()]}
    if method == "tools/call":
        text = TOOLS[params["name"]][0](**params.get("arguments", {}))
        return {"content": [{"type": "text", "text": text}], "isError": False}
    return {}


for line in sys.stdin:
    message = json.loads(line)
    if "id" not in message:
        continue
    result = handle(message["method"], message.get("params") or {})
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}) + "\n")
    sys.stdout.flush()
