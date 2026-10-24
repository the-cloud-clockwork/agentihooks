import json
import os
import subprocess
import sys
from functools import cache
from pathlib import Path


@cache
def get_catalog() -> dict[str, list[dict]]:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import asyncio
import json
from hooks.mcp import build_server
from hooks.mcp._registry import CATEGORY_MODULES

async def catalog():
    return {
        category: [
            tool.model_dump(by_alias=True, exclude_none=True)
            for tool in await build_server([category]).list_tools()
        ]
        for category in CATEGORY_MODULES
    }

print(json.dumps(asyncio.run(catalog())))
""",
        ],
        cwd=Path(__file__).resolve().parents[2],
        env=os.environ | {"ALLOWED_TOOLS": ""},
        stdout=subprocess.PIPE,
        text=True,
        check=True,
        timeout=30,
    )
    return json.loads(result.stdout)


CAPABILITIES = {
    "experimental": {},
    "prompts": {"listChanged": False},
    "resources": {"subscribe": False, "listChanged": False},
    "tools": {"listChanged": False},
}

PROTOCOL_VERSIONS = ["2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"]
