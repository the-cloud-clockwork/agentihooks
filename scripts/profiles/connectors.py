"""Which declared MCP servers a rendered profile mounts, with each server's tool filter in its harness's native form.

Codex takes ``enabled_tools`` and ``disabled_tools`` itself. Claude has no
per-server allowlist, so its render lists the server's advertised tools and
denies every one outside the allowlist; a server it cannot list stays unmounted.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path

from scripts.targets._common import (
    _install_module,
    drop_if_credentialed,
    resolve_env_references,
    sanitize_env_and_headers,
)
from scripts.targets.codex_target import codex_mcp_entry

CODEX_ONLY = ("enabled_tools", "disabled_tools", "default_tools_approval_mode")
CREDENTIALED = "credential-shaped literal in url, command or args"
NO_ALLOWLIST = "Claude has no native tool allowlist; only an http server's tools can be listed"
LIST_TIMEOUT_SECONDS = 30
_REFERENCE = re.compile(r"\$\{(\w+)[^}]*\}|\$(\w+)")


def path(name: str, target: str, root: Path) -> Path:
    return root / name / f"{target}.mounts.json"


async def _list(url: str, headers: dict) -> list[str]:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async with streamablehttp_client(url, headers=headers, timeout=LIST_TIMEOUT_SECONDS) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            names, cursor = [], None
            while True:
                page = await session.list_tools(cursor=cursor)
                names += [tool.name for tool in page.tools]
                cursor = page.nextCursor
                if not cursor:
                    return names


def advertised(url: str, headers: dict) -> list[str]:
    return asyncio.run(_list(url, headers))


def _resolved(headers: dict) -> tuple[dict, str]:
    out = {}
    for key, value in headers.items():
        text, ok = resolve_env_references(str(value))
        if not ok:
            names = [a or b for a, b in _REFERENCE.findall(str(value))]
            return {}, f"environment variable {next(n for n in names if n not in os.environ)} is unset"
        out[key] = text
    return out, ""


def require_environment(servers: dict) -> None:
    for name, spec in servers.items():
        if spec.get("enabled_tools") is not None:
            _, reason = _resolved(spec.get("headers") or {})
            if reason:
                raise ValueError(f"MCP '{name}' cannot mount: {reason}")


def _claude_filter(spec: dict) -> tuple[list[str], list[str], str]:
    """(tools to deny, allowlisted tools the server does not advertise, why it cannot mount)."""
    allow, disabled = spec.get("enabled_tools"), list(spec.get("disabled_tools") or [])
    if allow is None:
        return disabled, [], ""
    if not spec.get("url") or spec.get("type") == "sse":
        return [], [], NO_ALLOWLIST
    headers, reason = _resolved(spec.get("headers") or {})
    if reason:
        return [], [], reason
    try:
        tools = advertised(spec["url"], headers)
    except Exception as exc:
        return [], [], f"tool listing failed: {type(exc).__name__}: {exc}"
    denied = [tool for tool in tools if tool not in allow]
    return denied + [tool for tool in disabled if tool not in denied], [t for t in allow if t not in tools], ""


def _mounted(spec: dict, absent: list[str] | None = None) -> dict:
    row: dict = {"mounted": True}
    if spec.get("enabled_tools"):
        row["enabled_tools"] = list(spec["enabled_tools"])
    if absent:
        row["absent_tools"] = absent
    return row


def _unmounted(reason: str) -> dict:
    return {"mounted": False, "reason": reason}


def claude(servers: dict, dst: str) -> tuple[dict, list[str], dict]:
    """(servers for .claude.json, permission deny rules, mount manifest)."""
    mounted, deny, manifest = {}, [], {}
    for name, spec in servers.items():
        if drop_if_credentialed(name, spec, dst):
            manifest[name] = _unmounted(CREDENTIALED)
            continue
        denied, absent, reason = _claude_filter(spec)
        if reason:
            manifest[name] = _unmounted(reason)
            continue
        plain = {key: value for key, value in spec.items() if key not in CODEX_ONLY}
        mounted[name] = sanitize_env_and_headers(name, plain, dst)
        deny += [f"mcp__{name}__{tool}" for tool in denied]
        manifest[name] = _mounted(spec, absent)
    return mounted, deny, manifest


def codex(servers: dict) -> tuple[dict, dict]:
    """(entries for config.toml, mount manifest)."""
    mounted, manifest = {}, {}
    for name, spec in servers.items():
        entry, reason = codex_mcp_entry(name, dict(spec))
        if entry is None:
            manifest[name] = _unmounted(reason)
            continue
        mounted[name] = entry
        manifest[name] = _mounted(spec)
    return mounted, manifest


def write(dst: Path, manifest: dict, profile: str, target: str) -> None:
    say = _install_module()._cprint
    for name, row in manifest.items():
        if not row["mounted"]:
            say(f"  [!!] MCP '{name}' is not mounted for {profile} ({target}): {row['reason']}")
        elif row.get("absent_tools"):
            say(f"  [!!] MCP '{name}' does not advertise {', '.join(row['absent_tools'])} for {profile} ({target})")
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(json.dumps(manifest, indent=2) + "\n")
