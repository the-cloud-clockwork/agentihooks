import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from hooks.mcp._catalog import get_catalog

pytestmark = pytest.mark.unit


def test_registered_tool_appears_without_editing_startup_catalog(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_CATEGORIES", "channels")
    monkeypatch.delenv("ALLOWED_TOOLS", raising=False)
    assert "channel_list" in {tool["name"] for tool in get_catalog()["channels"]}
    source = Path(__file__).resolve().parents[1] / "hooks"
    shutil.copytree(source, tmp_path / "hooks", ignore=shutil.ignore_patterns("__pycache__"))
    module = tmp_path / "hooks" / "mcp" / "channels.py"
    module.write_text(
        module.read_text() + "\n    @mcp.tool()\n"
        "    def added_tool(message: str, count: int = 2) -> str:\n"
        '        """An added tool."""\n'
        "        return message * count\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", "import json; from hooks.mcp._stdio import list_tools; print(json.dumps(list_tools()))"],
        cwd=tmp_path,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    tools = {tool["name"]: tool for tool in json.loads(result.stdout)}
    assert tools["added_tool"] == {
        "name": "added_tool",
        "description": "An added tool.",
        "inputSchema": {
            "title": "added_toolArguments",
            "type": "object",
            "properties": {
                "message": {"title": "Message", "type": "string"},
                "count": {"default": 2, "title": "Count", "type": "integer"},
            },
            "required": ["message"],
        },
    }
