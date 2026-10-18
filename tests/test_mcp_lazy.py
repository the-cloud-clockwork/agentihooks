import asyncio
import json
import os
import select
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_RUNNER = """
import json, runpy, sys
try:
    runpy.run_module('hooks.mcp', run_name='__main__')
finally:
    print('IMPORTS=' + json.dumps(sorted(sys.modules)), file=sys.stderr)
"""


@contextmanager
def server(monkeypatch):
    monkeypatch.setenv("MCP_TRANSPORT", "stdio")
    monkeypatch.setenv("MCP_CATEGORIES", "all")
    monkeypatch.delenv("ALLOWED_TOOLS", raising=False)
    process = subprocess.Popen(
        [sys.executable, "-c", _RUNNER],
        cwd=Path(__file__).resolve().parents[1],
        env=os.environ.copy(),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    def request(method, params=None, request_id=1):
        message = {"jsonrpc": "2.0", "method": method, "id": request_id}
        if params is not None:
            message["params"] = params
        process.stdin.write(json.dumps(message) + "\n")
        process.stdin.flush()
        assert select.select([process.stdout], [], [], 30)[0], "No protocol reply"
        return json.loads(process.stdout.readline())

    try:
        yield request, process
    finally:
        process.stdin.close()
        try:
            process.wait(timeout=30)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()


def initialize(request):
    return request(
        "initialize",
        {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "lazy-start-contract", "version": "1"},
        },
    )["result"]


def test_startup_and_listing_do_not_load_protocol_or_hook_dispatch(monkeypatch):
    with server(monkeypatch) as (request, process):
        result = initialize(request)
        assert result["protocolVersion"] == "2025-03-26"
        listing = request("tools/list", request_id=2)["result"]["tools"]
        assert len(listing) == 13
        assert request("ping", request_id=3)["result"] == {}
    stderr = process.stderr.read()
    modules = json.loads(next(line[8:] for line in stderr.splitlines() if line.startswith("IMPORTS=")))
    assert "hooks.hook_manager" not in modules
    assert "hooks.context.classifier" not in modules
    assert not any(name == "mcp" or name.startswith("mcp.") for name in modules)
    assert not any(
        name in modules
        for name in (
            "hooks.mcp.channels",
            "hooks.mcp.enforcement",
            "hooks.mcp.conditions",
        )
    )
    from hooks.mcp import build_server

    expected = asyncio.run(build_server().list_tools())
    assert listing == [tool.model_dump(by_alias=True, exclude_none=True) for tool in expected]
