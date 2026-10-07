import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_mcp_tool_schemas_always_load_on_demand():
    env = json.loads((ROOT / "profiles/_base/settings.base.json").read_text())["env"]
    assert env["ENABLE_TOOL_SEARCH"] == "true"
