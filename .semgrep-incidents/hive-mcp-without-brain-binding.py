import os
from typing import Any

from scripts.profiles import browser
from scripts.swarm import brain_binding


def unbound(adapter: Any, servers: dict[str, Any], names: list[str]) -> None:
    # ruleid: hive-mcp-without-brain-binding
    adapter.register_mcp(browser.configure(servers, names))


def managed(adapter: Any, servers: dict[str, Any], names: list[str]) -> None:
    # ok: hive-mcp-without-brain-binding
    adapter.register_mcp(brain_binding.apply(browser.configure(servers, names), os.environ))
