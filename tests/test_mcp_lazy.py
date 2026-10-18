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
    monkeypatch.setenv("BRAIN_ENABLED", "false")
    monkeypatch.setenv("BRAIN_SOURCE_TYPE", "file")
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
        process.stdin = None
        try:
            _, process.stderr_output = process.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            raise


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
    stderr = process.stderr_output
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


def test_all_thirteen_tools_keep_their_results_after_lazy_start(monkeypatch):
    with server(monkeypatch) as (request, process):
        initialize(request)

        def call(name, arguments=None):
            reply = request("tools/call", {"name": name, "arguments": arguments or {}}, request_id=name)
            assert reply["id"] == name
            assert not reply["result"].get("isError", False)
            return json.loads(reply["result"]["content"][0]["text"])

        published = call("channel_publish", {"channel": "test", "message": "test message"})
        assert published["success"] is True
        assert call("channel_list") == {
            "success": True,
            "channels": {"test": 1},
            "global_messages": 0,
            "total_messages": 1,
        }
        assert call("channel_acknowledge", {"message_id": published["message_id"], "session_id": "test-session"}) == {
            "success": True,
            "message_id": published["message_id"],
            "acknowledged": True,
        }
        assert call("channel_clear", {"message_id": published["message_id"]}) == {"success": True, "cleared": 1}
        assert call("brain_refresh") == {"success": True, "published": False}
        assert call("brain_status")["success"] is True
        added = call("enforcement_set", {"message": "test enforcement", "tag": "test"})
        assert added["success"] is True
        listed = call("enforcement_list")
        assert listed["count"] == 1
        assert listed["enforcements"][0]["id"] == added["enforcement_id"]
        assert call("enforcement_clear", {"enforcement_id": added["enforcement_id"]}) == {"success": True, "cleared": 1}
        assert call("condition_list")["success"] is True
        assert call("condition_show", {"file": "missing.sh"}) == {
            "success": False,
            "error": "no active condition named 'missing.sh'",
        }
        refused = call(
            "condition_set",
            {
                "step": "pre",
                "matcher": "bash",
                "name": "test",
                "script": "exit 0",
                "session_id": "test-session",
            },
        )
        assert refused["success"] is False
        assert refused["error"]
        refused = call("condition_clear", {"file": "missing.sh", "session_id": "test-session"})
        assert refused["success"] is False
        assert refused["error"]
        assert request("tools/list", request_id="after-call")["result"]["tools"]
        invalid = request("tools/call", {"name": "channel_publish", "arguments": {}}, request_id="invalid")
        assert invalid["result"]["isError"] is True
        assert request("ping", request_id="after-error")["result"] == {}
    assert process.returncode == 0
    modules = json.loads(next(line[8:] for line in process.stderr_output.splitlines() if line.startswith("IMPORTS=")))
    assert "mcp.server.fastmcp" in modules
