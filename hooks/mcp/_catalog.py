import json
import os
import subprocess
import sys
from functools import cache
from pathlib import Path


@cache
def get_catalog() -> dict[str, dict[str, list[dict]]]:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import asyncio
import contextlib
import io
import json
from hooks.mcp import build_server
from hooks.mcp._registry import CATEGORY_MODULES

LISTINGS = {
    "tools": "list_tools",
    "resources": "list_resources",
    "resourceTemplates": "list_resource_templates",
    "prompts": "list_prompts",
}

async def catalog():
    with contextlib.redirect_stderr(io.StringIO()):
        servers = {category: build_server([category]) for category in CATEGORY_MODULES}
    return {
        kind: {
            category: [
                item.model_dump(by_alias=True, exclude_none=True)
                for item in await getattr(server, listing)()
            ]
            for category, server in servers.items()
        }
        for kind, listing in LISTINGS.items()
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
