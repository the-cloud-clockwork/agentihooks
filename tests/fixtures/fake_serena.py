"""Stand-in for `serena start-mcp-server`: same CLI shape, tools that touch only --project."""

import asyncio
import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

READ = ToolAnnotations(readOnlyHint=True)
WRITE = ToolAnnotations(readOnlyHint=False)

project = Path(sys.argv[sys.argv.index("--project") + 1]) if "--project" in sys.argv else None
mcp = FastMCP("fake-serena")


@mcp.tool(annotations=READ)
def find_symbol(name_path_pattern: str = "") -> str:
    return f"root={project}"


@mcp.tool(annotations=WRITE)
def replace_content(relative_path: str, repl: str) -> str:
    (project / relative_path).write_text(repl)
    return "OK"


@mcp.tool(annotations=READ)
async def slow(seconds: float = 2.0) -> str:
    await asyncio.sleep(seconds)
    return "slept"


@mcp.tool()
def mystery() -> str:
    return "unannotated"


@mcp.tool(annotations=READ)
def activate_project(project: str) -> str:
    return "the router must never forward this"


if __name__ == "__main__":
    mcp.run("stdio")
