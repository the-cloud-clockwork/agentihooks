import asyncio
import json
import os
import select
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryFile

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
    with TemporaryFile(dir=Path.home()) as stderr:
        process = subprocess.Popen(
            [sys.executable, "-c", _RUNNER],
            cwd=Path(__file__).resolve().parents[1],
            env=os.environ.copy(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr,
            bufsize=0,
        )

        def request(method, params=None, request_id=1):
            message = {"jsonrpc": "2.0", "method": method, "id": request_id}
            if params is not None:
                message["params"] = params
            process.stdin.write((json.dumps(message) + "\n").encode())
            process.stdin.flush()
            while True:
                assert select.select([process.stdout], [], [], 30)[0], "No protocol reply"
                reply = json.loads(process.stdout.readline())
                if reply.get("id") == request_id:
                    return reply

        try:
            yield request, process
        finally:
            process.stdin.close()
            process.stdin = None
            try:
                process.communicate(timeout=30)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
                raise
            stderr.seek(0)
            process.stderr_output = stderr.read().decode()


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
        for method, key in (
            ("resources/list", "resources"),
            ("resources/templates/list", "resourceTemplates"),
            ("prompts/list", "prompts"),
        ):
            assert request(method, request_id=method)["result"] == {key: []}
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


@pytest.mark.parametrize(
    "categories,allowed",
    [
        ("all", ""),
        ("channels,channels", "CHANNEL_LIST,brain_status"),
        ("enforcement,conditions", ""),
        ("unknown", ""),
        ("all", "missing"),
        ("all", ","),
    ],
)
def test_discovery_matches_registry_configuration(monkeypatch, capsys, categories, allowed):
    from hooks.mcp import build_server
    from hooks.mcp._stdio import list_tools

    monkeypatch.setenv("MCP_CATEGORIES", categories)
    monkeypatch.setenv("ALLOWED_TOOLS", allowed)
    expected = asyncio.run(build_server().list_tools())
    assert list_tools() == [tool.model_dump(by_alias=True, exclude_none=True) for tool in expected]
    if categories == "unknown" or allowed == "missing":
        assert "ZERO tools" in capsys.readouterr().err


@pytest.mark.parametrize("requested", ["2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25", "unknown"])
def test_discovery_handshake_matches_protocol_library(monkeypatch, capsys, requested):
    from io import StringIO

    from mcp.shared.version import SUPPORTED_PROTOCOL_VERSIONS
    from mcp.types import LATEST_PROTOCOL_VERSION

    from hooks.mcp import build_server
    from hooks.mcp._stdio import run

    monkeypatch.setenv("MCP_CATEGORIES", "all")
    monkeypatch.delenv("ALLOWED_TOOLS", raising=False)
    monkeypatch.setattr(
        sys,
        "stdin",
        StringIO(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": "init",
                    "method": "initialize",
                    "params": {
                        "protocolVersion": requested,
                        "capabilities": {},
                        "clientInfo": {"name": "contract", "version": "1"},
                    },
                }
            )
            + "\n"
            + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
            + "\n"
        ),
    )
    run()
    replies = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    expected = build_server()._mcp_server.create_initialization_options()
    assert replies[0] == {
        "jsonrpc": "2.0",
        "id": "init",
        "result": {
            "protocolVersion": requested if requested in SUPPORTED_PROTOCOL_VERSIONS else LATEST_PROTOCOL_VERSION,
            "capabilities": expected.capabilities.model_dump(by_alias=True, exclude_none=True),
            "serverInfo": {"name": expected.server_name, "version": expected.server_version},
        },
    }
    assert len(replies[1]["result"]["tools"]) == 13


@pytest.mark.parametrize(
    "invalid",
    [[], {"jsonrpc": "2.0", "id": 0, "method": "initialize"}, {"jsonrpc": "2.0", "id": 0, "method": []}],
)
def test_invalid_protocol_message_keeps_server_reading(monkeypatch, invalid):
    with server(monkeypatch) as (request, process):
        process.stdin.write((json.dumps(invalid) + "\n").encode())
        process.stdin.flush()
        assert initialize(request)["protocolVersion"] == "2025-03-26"
        assert request("ping", request_id="after-invalid")["result"] == {}
    assert process.returncode == 0


def test_first_call_transfers_initialized_session_to_protocol_library(monkeypatch, capsys):
    from io import StringIO

    from hooks.mcp._stdio import run

    messages = [
        {
            "jsonrpc": "2.0",
            "id": "init",
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "contract", "version": "1"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": "listing", "method": "tools/list"},
        {"jsonrpc": "2.0", "id": "call", "method": "tools/call", "params": {"name": "channel_list", "arguments": {}}},
    ]
    monkeypatch.setenv("MCP_CATEGORIES", "channels")
    monkeypatch.delenv("ALLOWED_TOOLS", raising=False)
    monkeypatch.setattr(sys, "stdin", StringIO("".join(json.dumps(message) + "\n" for message in messages)))
    run()
    replies = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [reply["id"] for reply in replies].count("init") == 1
    assert next(reply for reply in replies if reply["id"] == "listing")["result"]["tools"]
    result = next(reply for reply in replies if reply["id"] == "call")["result"]
    assert result["isError"] is False
    assert json.loads(result["content"][0]["text"]) == {
        "success": True,
        "channels": {},
        "global_messages": 0,
        "total_messages": 0,
    }


def test_hook_entry_point_keeps_its_public_export(monkeypatch):
    import hooks
    from hooks.hook_manager import main

    monkeypatch.delattr(hooks, "main", raising=False)
    assert hooks.main is main
    assert hooks.__dict__["main"] is main
    with pytest.raises(AttributeError, match="no attribute 'missing'"):
        hooks.missing
