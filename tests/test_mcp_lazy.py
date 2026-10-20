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

pytestmark = [pytest.mark.unit, pytest.mark.xdist_group("mcp-sdk")]

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
            "hooks.mcp.scheduler",
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
    registry = capsys.readouterr()
    assert list_tools() == [tool.model_dump(by_alias=True, exclude_none=True) for tool in expected]
    discovery = capsys.readouterr()
    assert discovery.out == ""
    assert discovery.err == registry.err
    if categories == "unknown" or allowed in ("missing", ","):
        assert "ZERO tools" in discovery.err
    else:
        assert "ZERO tools" not in discovery.err
    if categories == "unknown":
        assert "ignoring unknown MCP categories ['unknown']" in discovery.err


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
    assert len(replies[1]["result"]["tools"]) == 22


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


_INITIALIZE = {
    "jsonrpc": "2.0",
    "id": "init",
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-03-26",
        "capabilities": {},
        "clientInfo": {"name": "contract", "version": "1"},
    },
}
_INITIALIZED = {"jsonrpc": "2.0", "method": "notifications/initialized"}


class _Stdout:
    def __init__(self):
        self.text = ""
        self.flushes = []

    def write(self, text):
        self.text += text

    def flush(self):
        self.flushes.append(self.text.count("\n"))


def _run(monkeypatch, messages):
    from io import StringIO

    from hooks.mcp import _stdio

    transfers = []

    async def serve(history, stdin, stdout, initialize_id):
        transfers.append((history, initialize_id))

    lines = [message if isinstance(message, str) else json.dumps(message) + "\n" for message in messages]
    stdout = _Stdout()
    monkeypatch.setenv("MCP_CATEGORIES", "channels")
    monkeypatch.delenv("ALLOWED_TOOLS", raising=False)
    monkeypatch.setattr(_stdio, "_serve", serve)
    monkeypatch.setattr(sys, "stdin", StringIO("".join(lines)))
    monkeypatch.setattr(sys, "stdout", stdout)
    _stdio.run()
    return lines, [json.loads(line) for line in stdout.text.splitlines()], transfers, stdout.flushes


def test_discovery_requests_are_answered_and_flushed_without_the_protocol_library(monkeypatch):
    from importlib.metadata import version

    from hooks.mcp._stdio import list_tools

    _, replies, transfers, flushes = _run(
        monkeypatch,
        [
            _INITIALIZE,
            _INITIALIZED,
            {"jsonrpc": "2.0", "id": 1, "method": "ping"},
            {"jsonrpc": "2.0", "id": 2, "method": "resources/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "resources/templates/list"},
            {"jsonrpc": "2.0", "id": 4, "method": "prompts/list"},
            {"jsonrpc": "2.0", "id": 5, "method": "tools/list"},
        ],
    )
    assert transfers == []
    assert flushes == [1, 2, 3, 4, 5, 6]
    assert replies[0]["result"]["serverInfo"] == {"name": "agentihooks", "version": version("mcp")}
    assert [{key: reply[key] for key in ("jsonrpc", "id", "result")} for reply in replies[1:]] == [
        {"jsonrpc": "2.0", "id": 1, "result": {}},
        {"jsonrpc": "2.0", "id": 2, "result": {"resources": []}},
        {"jsonrpc": "2.0", "id": 3, "result": {"resourceTemplates": []}},
        {"jsonrpc": "2.0", "id": 4, "result": {"prompts": []}},
        {"jsonrpc": "2.0", "id": 5, "result": {"tools": list_tools()}},
    ]


@pytest.mark.parametrize(
    "first",
    [
        {"jsonrpc": "2.0", "id": 1, "method": "resources/list"},
        {"jsonrpc": "2.0", "id": 1, "method": "resources/templates/list"},
        {"jsonrpc": "2.0", "id": 1, "method": "prompts/list"},
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "channel_list"}},
        {"jsonrpc": "2.0", "method": "ping"},
        "[]\n",
        "not json\n",
    ],
)
def test_request_before_initialize_moves_to_the_protocol_library_alone(monkeypatch, first):
    lines, replies, transfers, _ = _run(monkeypatch, [first])
    assert replies == []
    assert transfers == [(lines, None)]


def test_first_call_moves_the_initialized_session_to_the_protocol_library(monkeypatch):
    call = {"jsonrpc": "2.0", "id": "call", "method": "tools/call", "params": {"name": "channel_list"}}
    lines, replies, transfers, _ = _run(monkeypatch, [_INITIALIZE, _INITIALIZED, call])
    assert [reply["id"] for reply in replies] == ["init"]
    assert transfers == [(lines, "init")]


@pytest.mark.parametrize(
    "line,valid",
    [
        ("not json", False),
        ("[]", False),
        ({"jsonrpc": "1.0", "id": 1, "method": "ping"}, False),
        ({"jsonrpc": "2.0", "id": 1, "method": "ping", "params": []}, False),
        ({"jsonrpc": "2.0", "id": 1, "method": []}, False),
        ({"jsonrpc": "2.0", "id": 1, "method": "ping"}, True),
        ({"jsonrpc": "2.0", "id": "a", "method": "ping", "params": {}}, True),
        ({"jsonrpc": "2.0", "method": "ping"}, False),
        ({"jsonrpc": "2.0", "id": True, "method": "ping"}, False),
        ({"jsonrpc": "2.0", "id": 1.5, "method": "ping"}, False),
        (_INITIALIZED, True),
        ({**_INITIALIZED, "id": True}, False),
        (_INITIALIZE, True),
        ({**_INITIALIZE, "params": {"protocolVersion": "2025-03-26", "capabilities": {}}}, False),
        ({**_INITIALIZE, "params": {**_INITIALIZE["params"], "clientInfo": []}}, False),
        ({**_INITIALIZE, "params": {**_INITIALIZE["params"], "clientInfo": {"name": "contract"}}}, False),
        ({**_INITIALIZE, "params": {**_INITIALIZE["params"], "protocolVersion": 1}}, False),
        ({**_INITIALIZE, "params": {**_INITIALIZE["params"], "capabilities": None}}, False),
    ],
)
def test_request_validation_matches_the_handshake_contract(line, valid):
    from hooks.mcp._stdio import _request

    text = line if isinstance(line, str) else json.dumps(line)
    assert _request(text) == (json.loads(text) if valid else None)


def test_output_drops_only_the_replayed_initialize_reply():
    from hooks.mcp._stdio import _Output

    class Stream:
        def __init__(self):
            self.ids = []
            self.flushed = 0

        async def write(self, data):
            self.ids.append(json.loads(data)["id"])

        async def flush(self):
            self.flushed += 1

    async def scenario(initialize_id):
        stream = Stream()
        output = _Output(stream, initialize_id)
        for request_id in ("other", "init", "init", ""):
            await output.write(json.dumps({"id": request_id}))
        await output.flush()
        return stream

    replayed = asyncio.run(scenario("init"))
    assert replayed.ids == ["other", "init", ""]
    assert replayed.flushed == 1
    assert asyncio.run(scenario(None)).ids == ["other", "init", "init", ""]


def test_protocol_library_answers_initialize_with_the_server_options(monkeypatch):
    from io import StringIO

    from hooks.mcp import build_server
    from hooks.mcp._stdio import _serve

    monkeypatch.setenv("MCP_CATEGORIES", "channels")
    stdout = StringIO()
    asyncio.run(_serve([json.dumps(_INITIALIZE) + "\n"], StringIO(""), stdout, None))
    reply = json.loads(stdout.getvalue().splitlines()[0])
    expected = build_server()._mcp_server.create_initialization_options()
    assert reply["id"] == "init"
    assert reply["result"]["serverInfo"] == {"name": expected.server_name, "version": expected.server_version}
