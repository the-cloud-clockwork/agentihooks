import json
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.profiles import connectors

pytestmark = [pytest.mark.unit, pytest.mark.xdist_group("mcp-sdk")]

READS = ["lf-a", "lf-b"]
NO_ALLOWLIST = "Claude has no native tool allowlist; only an http server's tools can be listed"


@pytest.fixture
def said(monkeypatch):
    from scripts.targets._common import _install_module

    lines: list[str] = []
    monkeypatch.setattr(_install_module(), "_cprint", lambda msg, **kwargs: lines.append(msg))
    return lines


@pytest.fixture
def listing(monkeypatch):
    calls = []

    def fake(url, headers):
        calls.append((url, headers))
        return ["lf-a", "lf-b", "lf-c", "lf-d"]

    monkeypatch.setattr(connectors, "advertised", fake)
    return calls


def test_path_sits_beside_the_profile_homes(tmp_path):
    assert connectors.path("qa", "codex", tmp_path) == tmp_path / "qa" / "codex.mounts.json"


def test_list_walks_every_page_with_the_given_headers(monkeypatch):
    import mcp
    import mcp.client.streamable_http as transport

    seen = {}
    pages = {None: (["a", "b"], "p2"), "p2": (["c"], "p3"), "p3": ([], None)}

    @asynccontextmanager
    async def client(url, headers=None, timeout=None):
        seen["client"] = (url, headers, timeout)
        yield "r", "w", None

    class Session:
        def __init__(self, read, write):
            seen["streams"] = (read, write)
            seen["cursors"] = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def initialize(self):
            seen["initialized"] = True

        async def list_tools(self, cursor=None):
            seen["cursors"].append(cursor)
            assert len(seen["cursors"]) <= len(pages)
            names, nxt = pages[cursor]
            return SimpleNamespace(tools=[SimpleNamespace(name=n) for n in names], nextCursor=nxt)

    monkeypatch.setattr(transport, "streamablehttp_client", client)
    monkeypatch.setattr(mcp, "ClientSession", Session)

    assert connectors.advertised("https://g.example/mcp", {"H": "v"}) == ["a", "b", "c"]
    assert seen == {
        "client": ("https://g.example/mcp", {"H": "v"}, 30),
        "streams": ("r", "w"),
        "cursors": [None, "p2", "p3"],
        "initialized": True,
    }


def test_resolved_expands_references_in_memory(monkeypatch):
    monkeypatch.setenv("K", "kv")
    monkeypatch.setenv("B", "bv")
    monkeypatch.delenv("UNSET", raising=False)
    headers = {"A": "Bearer ${K}", "B": "$B", "D": "${UNSET:-dflt}", "L": "lit"}
    assert connectors._resolved(headers) == ({"A": "Bearer kv", "B": "bv", "D": "dflt", "L": "lit"}, "")


@pytest.mark.parametrize("value", ["${K}-${MISSING}", "$K$MISSING"])
def test_resolved_names_the_unset_variable(monkeypatch, value):
    monkeypatch.setenv("K", "kv")
    monkeypatch.delenv("MISSING", raising=False)
    assert connectors._resolved({"A": "lit", "B": value}) == ({}, "environment variable MISSING is unset")


def test_claude_filter_without_allowlist_denies_only_disabled(listing):
    assert connectors._claude_filter({"url": "u", "disabled_tools": ("x",)}) == (["x"], [], "")
    assert connectors._claude_filter({"command": "c"}) == ([], [], "")
    assert listing == []


@pytest.mark.parametrize(
    "spec",
    [
        {"command": "c", "enabled_tools": READS},
        {"type": "sse", "url": "http://x/sse", "enabled_tools": READS},
    ],
)
def test_claude_filter_cannot_list_a_non_http_server(listing, spec):
    assert connectors._claude_filter(spec) == ([], [], NO_ALLOWLIST)
    assert listing == []


def test_claude_filter_denies_the_complement_and_names_absent_tools(listing, monkeypatch):
    monkeypatch.setenv("GW", "g")
    spec = {
        "url": "u",
        "headers": {"A": "${GW}"},
        "enabled_tools": ["lf-a", "lf-z"],
        "disabled_tools": ["lf-c", "lf-y"],
    }
    assert connectors._claude_filter(spec) == (["lf-b", "lf-c", "lf-d", "lf-y"], ["lf-z"], "")
    assert listing == [("u", {"A": "g"})]


def test_claude_filter_reports_an_unset_reference_before_listing(listing, monkeypatch):
    monkeypatch.delenv("GW", raising=False)
    spec = {"url": "u", "headers": {"A": "${GW}"}, "enabled_tools": READS}
    assert connectors._claude_filter(spec) == ([], [], "environment variable GW is unset")
    assert listing == []


def test_claude_filter_reports_a_failed_listing(monkeypatch):
    def refuse(url, headers):
        raise ConnectionError("refused")

    monkeypatch.setattr(connectors, "advertised", refuse)
    spec = {"url": "u", "enabled_tools": READS}
    assert connectors._claude_filter(spec) == ([], [], "tool listing failed: ConnectionError: refused")


def test_claude_mounts_plain_entries_and_collects_deny_rules(listing, said):
    leak = "ghp_" + "a" * 36
    servers = {
        "leaky": {"command": "s", "args": ["--token", leak]},
        "stdio": {"command": "s", "enabled_tools": READS},
        "gw": {"type": "http", "url": "u", "headers": {"A": "${GW_REF:-x}"}, "enabled_tools": ["lf-a", "lf-z"]},
        "plain": {"command": "p", "disabled_tools": ["t"], "default_tools_approval_mode": "approve"},
    }
    mounted, deny, manifest = connectors.claude(servers, "dst.json")
    assert mounted == {
        "gw": {"type": "http", "url": "u", "headers": {"A": "${GW_REF:-x}"}},
        "plain": {"command": "p"},
    }
    assert deny == ["mcp__gw__lf-b", "mcp__gw__lf-c", "mcp__gw__lf-d", "mcp__plain__t"]
    assert manifest == {
        "leaky": {"mounted": False, "reason": "credential-shaped literal in url, command or args"},
        "stdio": {"mounted": False, "reason": NO_ALLOWLIST},
        "gw": {"mounted": True, "enabled_tools": ["lf-a", "lf-z"], "absent_tools": ["lf-z"]},
        "plain": {"mounted": True},
    }


def test_claude_mount_drops_a_credential_header_but_keeps_references(listing, said):
    dummy = "AKIA" + "TESTDUMMY0000000"
    mounted, _, _ = connectors.claude({"gw": {"url": "u", "headers": {"K": dummy, "R": "${R}"}}}, "dst.json")
    assert mounted == {"gw": {"url": "u", "headers": {"R": "${R}"}}}


def test_mounted_rows():
    assert connectors._mounted({"enabled_tools": ("a",)}) == {"mounted": True, "enabled_tools": ["a"]}
    assert connectors._mounted({"enabled_tools": []}, []) == {"mounted": True}
    assert connectors._mounted({}, ["z"]) == {"mounted": True, "absent_tools": ["z"]}
    assert connectors._unmounted("why") == {"mounted": False, "reason": "why"}


def test_codex_translates_each_declared_server(said):
    servers = {
        "gw": {"url": "u", "headers": {"Authorization": "Bearer ${GW}"}, "enabled_tools": READS},
        "old": {"type": "sse", "url": "http://x/sse"},
    }
    mounted, manifest = connectors.codex(servers)
    assert mounted == {"gw": {"url": "u", "bearer_token_env_var": "GW", "enabled_tools": READS}}
    assert manifest == {
        "gw": {"mounted": True, "enabled_tools": READS},
        "old": {"mounted": False, "reason": "codex has no SSE transport"},
    }
    assert said == [
        "  [!!] MCP 'old' uses SSE — codex has no SSE transport; skipped. "
        "Expose a streamable-HTTP endpoint and re-run init."
    ]


def test_write_records_the_manifest_and_names_every_gap(tmp_path, said):
    manifest = {
        "ok": {"mounted": True},
        "gw": {"mounted": True, "enabled_tools": ["a", "z"], "absent_tools": ["z", "y"]},
        "old": {"mounted": False, "reason": "codex has no SSE transport"},
    }
    dst = tmp_path / "root" / "qa" / "codex.mounts.json"
    connectors.write(dst, manifest, "qa", "codex")
    assert dst.read_text() == json.dumps(manifest, indent=2) + "\n"
    assert said == [
        "  [!!] MCP 'gw' does not advertise z, y for qa (codex)",
        "  [!!] MCP 'old' is not mounted for qa (codex): codex has no SSE transport",
    ]


def test_write_replaces_an_older_manifest(tmp_path, said):
    dst = Path(tmp_path) / "m.json"
    dst.write_text("old")
    connectors.write(dst, {}, "qa", "claude")
    assert dst.read_text() == "{}\n"
    assert said == []
