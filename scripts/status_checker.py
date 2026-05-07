#!/usr/bin/env python3
"""Status checker — shared diagnostics for CLI and skill surfaces.

Usage:
    python3 scripts/status_checker.py                  # colored terminal output
    python3 scripts/status_checker.py --json            # JSON for piping
    python3 scripts/status_checker.py --session <sid>   # include session metrics
"""

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Optional

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

AGENTIHOOKS_HOME = Path(os.getenv("AGENTIHOOKS_HOME", str(Path.home() / ".agentihooks")))
CLAUDE_HOME = Path(os.getenv("AGENTIHOOKS_CLAUDE_HOME", str(Path.home() / ".claude")))
STATE_JSON = AGENTIHOOKS_HOME / "state.json"

# ── Color tags (matching install.py conventions) ────────────────────────

_GREEN = "\033[32m"
_DIM = "\033[2m"
_YELLOW = "\033[33m"
_RED = "\033[31m"
_RESET = "\033[0m"

_TAG_COLORS = {
    "[OK]": _GREEN,
    "[--]": _DIM,
    "[!!]": _YELLOW,
    "[RM]": _RED,
}


def _cprint(msg: str) -> str:
    """Apply color to a status-tagged line and return it."""
    for tag, color in _TAG_COLORS.items():
        if tag in msg:
            return f"{color}{msg}{_RESET}"
    return msg


# ── Helpers ─────────────────────────────────────────────────────────────


def _load_state() -> dict:
    if STATE_JSON.exists():
        try:
            return json.loads(STATE_JSON.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


# ── Individual check functions ──────────────────────────────────────────


def check_profile() -> dict[str, Any]:
    state = _load_state()
    global_target = state.get("targets", {}).get("global", {})
    name = global_target.get("profile", "")
    settings_profile = global_target.get("settings_profile", "")
    bundle = state.get("bundle", {}).get("path", "")
    bundle_ok = bool(bundle and Path(bundle).expanduser().exists())
    raw_links = state.get("linked_profiles", []) or []
    chain = [p.strip() for p in name.split(",") if p.strip()]
    linked: list[dict[str, Any]] = []
    for entry in raw_links:
        if not isinstance(entry, dict):
            continue
        lname = entry.get("name", "")
        lpath = entry.get("path", "")
        linked.append(
            {
                "name": lname,
                "path": lpath,
                "in_chain": lname in chain,
                "exists": bool(lpath and Path(lpath).expanduser().exists()),
            }
        )
    return {
        "name": name or "(not installed)",
        "settings_profile": settings_profile,
        "bundle": bundle or "(none)",
        "bundle_ok": bundle_ok,
        "linked_profiles": linked,
        "ok": bool(name),
    }


def check_hooks() -> dict[str, Any]:
    settings_path = CLAUDE_HOME / "settings.json"
    if not settings_path.exists():
        return {"total": 0, "expected": 10, "ok": False}
    try:
        data = json.loads(settings_path.read_text(encoding="utf-8"))
        hooks = data.get("hooks", {})
        total = len(hooks)
        return {"total": total, "expected": 10, "ok": total >= 10}
    except (json.JSONDecodeError, OSError):
        return {"total": 0, "expected": 10, "ok": False}


def check_python() -> dict[str, Any]:
    """Verify the Python binary actually used by hook commands in settings.json."""
    import subprocess

    # Extract the real Python from hook commands in settings.json
    python_path = _extract_hook_python()
    if not python_path:
        return {"path": "(not found in settings.json)", "ok": False}

    # Test if the binary actually runs
    try:
        result = subprocess.run(
            [python_path, "--version"],
            capture_output=True,
            timeout=5,
        )
        version = result.stdout.decode().strip() or result.stderr.decode().strip()
        return {"path": python_path, "version": version, "ok": result.returncode == 0}
    except (OSError, subprocess.TimeoutExpired):
        return {"path": python_path, "ok": False}


def _extract_hook_python() -> Optional[str]:
    """Extract Python path from hook commands in ~/.claude/settings.json."""
    import re

    settings_path = CLAUDE_HOME / "settings.json"
    if not settings_path.exists():
        return None
    try:
        data = json.loads(settings_path.read_text(encoding="utf-8"))
        hooks = data.get("hooks", {})
        for event_handlers in hooks.values():
            entries = event_handlers if isinstance(event_handlers, list) else [event_handlers]
            for entry in entries:
                for hook in entry.get("hooks", []) if isinstance(entry, dict) else []:
                    cmd = hook.get("command", "")
                    # Match: /path/to/python3 -m hooks or /path/to/python -m hooks
                    m = re.search(r"([\w/.+-]+python[\w.]*)\s+-m\s+hooks", cmd)
                    if m:
                        return m.group(1)
    except (json.JSONDecodeError, OSError):
        pass
    return None


def check_redis() -> dict[str, Any]:
    try:
        from hooks._redis import get_redis

        r = get_redis()
        if r is None:
            return {"connected": False, "keys": {}, "ok": False}
        r.ping()
        # Categorize agenticore keys by type
        counts: dict[str, int] = {}
        cursor = 0
        while True:
            cursor, keys = r.scan(cursor, match="agenticore:*", count=500)
            for k in keys:
                prefix = k.split(":")[1] if ":" in k else "other"
                counts[prefix] = counts.get(prefix, 0) + 1
            if cursor == 0:
                break
        total = sum(counts.values())
        return {"connected": True, "total_keys": total, "keys": counts, "ok": True}
    except Exception:
        return {"connected": False, "keys": {}, "ok": False}


def check_otel() -> dict[str, Any]:
    try:
        from hooks.config import OTEL_HOOKS_ENABLED

        return {"enabled": OTEL_HOOKS_ENABLED, "ok": True}
    except Exception:
        return {"enabled": False, "ok": True}


_GUARDRAIL_DESCRIPTIONS = {
    "bash_filter": "Truncates verbose bash output (docker, git log, test runners)",
    "file_dedup": "Blocks re-reading unchanged files already in context",
    "context_refresh": "Re-injects rules and CLAUDE.md every N turns for attention decay",
    "context_compression": "Token compression on injected content (standard level)",
    "context_audit": "Tracks per-tool token consumption across the session",
    "effort_policy": "Injects thinking/effort guidance, warns on expensive subagents",
    "peak_hours": "Shows peak billing indicator on statusline",
    "compact_suggest": "Smart /compact suggestions based on audit data",
}


def check_guardrails() -> dict[str, Any]:
    flags = {}
    try:
        from hooks.config import (
            BASH_FILTER_ENABLED,
            COMPACT_SUGGEST_ENABLED,
            CONTEXT_AUDIT_ENABLED,
            CONTEXT_REFRESH_COMPRESSION,
            CONTEXT_REFRESH_ENABLED,
            EFFORT_POLICY_ENABLED,
            FILE_READ_CACHE_ENABLED,
            PEAK_HOURS_ENABLED,
        )

        flags = {
            "bash_filter": BASH_FILTER_ENABLED,
            "file_dedup": FILE_READ_CACHE_ENABLED,
            "context_refresh": CONTEXT_REFRESH_ENABLED,
            "context_compression": CONTEXT_REFRESH_COMPRESSION != "off",
            "context_audit": CONTEXT_AUDIT_ENABLED,
            "effort_policy": EFFORT_POLICY_ENABLED,
            "peak_hours": PEAK_HOURS_ENABLED,
            "compact_suggest": COMPACT_SUGGEST_ENABLED,
        }
    except Exception:
        pass
    active = sum(1 for v in flags.values() if v)
    return {
        "active": active,
        "total": len(flags) or 6,
        "details": flags,
        "descriptions": _GUARDRAIL_DESCRIPTIONS,
        "ok": active > 0,
    }


def _resolve_env_refs(s: str) -> str:
    """Resolve ${VAR} references in a string from os.environ."""
    import re

    def _sub(m: "re.Match") -> str:
        return os.environ.get(m.group(1), m.group(0))

    return re.sub(r"\$\{(\w+)\}", _sub, s)


def _query_mcp_tools(url: str, headers: dict[str, str]) -> Optional[int]:
    """Query an HTTP MCP server for its tool count via initialize + tools/list."""
    try:
        import requests  # type: ignore
    except ImportError:
        return None

    resolved_headers = {k: _resolve_env_refs(v) for k, v in headers.items()}
    resolved_headers.setdefault("Content-Type", "application/json")
    resolved_headers.setdefault("Accept", "application/json, text/event-stream")

    try:
        # Initialize
        init_resp = requests.post(
            url,
            headers=resolved_headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "agentihooks-status", "version": "1.0"},
                },
            },
            timeout=5,
        )
        if init_resp.status_code != 200:
            return None

        # Extract session ID if present
        sid = init_resp.headers.get("Mcp-Session-Id", "")
        if sid:
            resolved_headers["Mcp-Session-Id"] = sid

        # tools/list
        tools_resp = requests.post(
            url,
            headers=resolved_headers,
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/list",
            },
            timeout=5,
        )
        if tools_resp.status_code != 200:
            return None

        # Parse SSE or JSON response
        text = tools_resp.text
        for line in text.splitlines():
            if line.startswith("data:"):
                data = json.loads(line[5:])
                return len(data.get("result", {}).get("tools", []))
        # Try direct JSON
        data = tools_resp.json()
        return len(data.get("result", {}).get("tools", []))
    except Exception:
        return None


def _count_hooks_utils_tools() -> int:
    """Count tools from hooks-utils by building the MCP server."""
    try:
        from hooks.mcp import build_server

        server = build_server()
        if hasattr(server, "_tool_manager"):
            return len(server._tool_manager._tools)
        return 0
    except Exception:
        return 0


_MCP_CACHE_FILE = AGENTIHOOKS_HOME / "mcp-tool-cache.json"
_MCP_CACHE_TTL = 3600  # 1 hour


def _load_tool_cache() -> dict[str, Any]:
    """Load cached tool counts. Returns {} if missing or expired."""
    if not _MCP_CACHE_FILE.exists():
        return {}
    try:
        data = json.loads(_MCP_CACHE_FILE.read_text(encoding="utf-8"))
        from datetime import datetime, timezone

        cached_at = datetime.fromisoformat(data.get("_cached_at", "2000-01-01T00:00:00+00:00"))
        age = (datetime.now(timezone.utc) - cached_at).total_seconds()
        if age > _MCP_CACHE_TTL:
            return {}  # expired
        return data.get("servers", {})
    except Exception:
        return {}


def _save_tool_cache(server_tools: dict[str, Optional[int]]) -> None:
    """Persist tool counts to cache file."""
    from datetime import datetime, timezone

    try:
        AGENTIHOOKS_HOME.mkdir(parents=True, exist_ok=True)
        _MCP_CACHE_FILE.write_text(
            json.dumps(
                {
                    "_cached_at": datetime.now(timezone.utc).isoformat(),
                    "servers": {k: v for k, v in server_tools.items() if v is not None},
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    except OSError:
        pass


def check_mcp() -> dict[str, Any]:
    """Full MCP state: servers, enabled/disabled per project, real tool counts for all."""
    try:
        user_mcp_path = Path.home() / ".claude.json"
        user_data = {}
        if user_mcp_path.exists():
            user_data = json.loads(user_mcp_path.read_text(encoding="utf-8"))

        # User-scope servers
        user_servers = user_data.get("mcpServers", {})

        # Project-scope servers (CWD .mcp.json)
        project_servers: dict[str, dict] = {}
        proj_mcp = Path.cwd() / ".mcp.json"
        if proj_mcp.exists():
            try:
                project_servers = json.loads(proj_mcp.read_text(encoding="utf-8")).get("mcpServers", {})
            except (json.JSONDecodeError, OSError):
                pass

        # Per-project disabled state (UI toggles via /mcp) from ~/.claude.json
        cwd = str(Path.cwd())
        project_block = user_data.get("projects", {}).get(cwd, {})
        disabled: set[str] = set(project_block.get("disabledMcpServers", []))
        for s in user_data.get("disabledMcpServers", []):
            disabled.add(s)
        local_settings = Path.cwd() / ".claude" / "settings.local.json"
        if local_settings.exists():
            try:
                for s in json.loads(local_settings.read_text(encoding="utf-8")).get("disabledMcpServers", []):
                    disabled.add(s)
            except (json.JSONDecodeError, OSError):
                pass

        # Load cache for tool counts
        cache = _load_tool_cache()
        all_cfg = {**user_servers, **project_servers}

        # Query ALL servers for tool counts (enabled or not)
        server_details = {}
        queried_tools: dict[str, Optional[int]] = {}
        fleet_tools = 0
        active_tools = 0

        for name, cfg in all_cfg.items():
            stype = "http" if cfg.get("url") else "stdio"
            source = "project" if name in project_servers else "user"
            enabled = name not in disabled

            # Get tool count: live query → cache fallback
            tools = None
            if name == "hooks-utils":
                tools = _count_hooks_utils_tools()
            elif stype == "http":
                # Try cache first
                if name in cache:
                    tools = cache[name]
                else:
                    url = cfg.get("url", "")
                    headers = cfg.get("headers", {})
                    tools = _query_mcp_tools(url, headers)
            # stdio servers other than hooks-utils: check cache only
            elif name in cache:
                tools = cache[name]

            queried_tools[name] = tools
            server_details[name] = {
                "type": stype,
                "source": source,
                "enabled": enabled,
                "tools": tools,
            }
            if tools is not None:
                fleet_tools += tools
                if enabled:
                    active_tools += tools

        # Save fresh cache
        _save_tool_cache(queried_tools)

        enabled_count = sum(1 for v in server_details.values() if v["enabled"])
        disabled_count = sum(1 for v in server_details.values() if not v["enabled"])

        return {
            "total": len(server_details),
            "enabled": enabled_count,
            "disabled": disabled_count,
            "fleet_tools": fleet_tools,
            "active_tools": active_tools,
            "servers": server_details,
            "ok": len(server_details) > 0,
        }
    except Exception as e:
        return {"total": 0, "enabled": 0, "disabled": 0, "servers": {}, "ok": False, "error": str(e)}


def check_quota() -> dict[str, Any]:
    try:
        from hooks.config import PEAK_HOURS_END, PEAK_HOURS_START, PEAK_HOURS_TZ
        from hooks.observability.peak_hours import peak_indicator

        peak = peak_indicator(PEAK_HOURS_START, PEAK_HOURS_END, PEAK_HOURS_TZ)
        return {"summary": "native (via statusline)", "peak": peak, "ok": True}
    except Exception as e:
        return {"summary": f"(error: {e})", "peak": "unknown", "ok": False}


def check_session(session_id: str) -> dict[str, Any]:
    result: dict[str, Any] = {"id": session_id, "ok": False}
    try:
        from hooks._redis import get_redis, redis_key

        r = get_redis()
        if r is None:
            result["error"] = "Redis unavailable"
            # Try in-memory fallback for context audit
            try:
                from hooks.observability.context_audit import get_audit_summary

                audit = get_audit_summary(session_id)
                if audit:
                    result["tool_audit"] = audit
                    result["ok"] = True
            except Exception:
                pass
            return result

        # Token metrics
        token_key = redis_key("tokens", session_id)
        metrics = r.hgetall(token_key)
        if metrics:
            result["fill_pct"] = float(metrics.get("fill_pct", 0))
            result["burn_rate"] = int(float(metrics.get("burn_rate", 0)))
            result["used"] = int(float(metrics.get("used", 0)))
            result["remaining"] = int(float(metrics.get("remaining", 0)))
            result["ok"] = True

        # Context audit
        try:
            from hooks.observability.context_audit import get_audit_summary

            audit = get_audit_summary(session_id)
            if audit:
                result["tool_audit"] = audit
        except Exception:
            pass

        # Warn state
        warn_key = redis_key("token_warn", session_id)
        warn_level = r.hget(warn_key, "level")
        if warn_level:
            result["warn_level"] = warn_level

        return result
    except Exception as e:
        result["error"] = str(e)
        return result


# ── Orchestrator ────────────────────────────────────────────────────────


def check_broadcast() -> dict[str, Any]:
    try:
        from hooks.config import BROADCAST_ENABLED

        if not BROADCAST_ENABLED:
            return {"enabled": False, "sessions": 0, "messages": 0, "ok": True}
        from hooks.context.broadcast import get_active_sessions, list_broadcasts

        sessions = get_active_sessions(cleanup=True)
        msgs = list_broadcasts()
        return {"enabled": True, "sessions": len(sessions), "messages": len(msgs), "ok": True}
    except Exception:
        return {"enabled": False, "sessions": 0, "messages": 0, "ok": True}


def run_all_checks(session_id: Optional[str] = None) -> dict[str, Any]:
    results: dict[str, Any] = {
        "profile": check_profile(),
        "hooks": check_hooks(),
        "python": check_python(),
        "redis": check_redis(),
        "otel": check_otel(),
        "guardrails": check_guardrails(),
        "mcp": check_mcp(),
        "quota": check_quota(),
        "broadcast": check_broadcast(),
    }
    if session_id:
        results["session"] = check_session(session_id)
    return results


# ── Formatters ──────────────────────────────────────────────────────────


def _fmt_bytes(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def format_cli(results: dict[str, Any]) -> str:
    lines = []

    # Profile
    p = results["profile"]
    tag = "[OK]" if p["ok"] else "[!!]"
    bundle_str = f" (bundle: {p['bundle']})" if p["bundle"] != "(none)" else ""
    sp_str = f" | settings: {p['settings_profile']}" if p.get("settings_profile") else ""
    lines.append(_cprint(f"{tag} Profile: {p['name']}{sp_str}{bundle_str}"))
    for lp in p.get("linked_profiles", []) or []:
        in_chain = "in chain" if lp["in_chain"] else "NOT in chain"
        exists = "" if lp["exists"] else " [MISSING]"
        lines.append(_cprint(f"     + linked: {lp['name']} -> {lp['path']} ({in_chain}){exists}"))

    # Hooks
    h = results["hooks"]
    tag = "[OK]" if h["ok"] else "[!!]"
    lines.append(_cprint(f"{tag} Hooks: {h['total']}/{h['expected']} wired in settings.json"))

    # Python
    py = results["python"]
    tag = "[OK]" if py["ok"] else "[!!]"
    version = f" ({py['version']})" if py.get("version") else ""
    lines.append(_cprint(f"{tag} Python: {py['path']}{version}"))

    # Redis
    r = results["redis"]
    if r["connected"]:
        total = r.get("total_keys", 0)
        key_parts = []
        for ktype, kcount in sorted(r.get("keys", {}).items(), key=lambda x: x[1], reverse=True):
            key_parts.append(f"{ktype}: {kcount}")
        detail = f"{total} keys" + (f" ({', '.join(key_parts)})" if key_parts else "")
        lines.append(_cprint(f"[OK] Redis: connected — {detail}"))
    else:
        lines.append(_cprint("[!!] Redis: not connected (in-memory fallback active)"))

    # OTEL
    o = results["otel"]
    if o["enabled"]:
        lines.append(_cprint("[OK] OTEL: enabled"))
    else:
        lines.append(_cprint("[--] OTEL: disabled"))

    # Broadcast
    b = results.get("broadcast", {})
    if b.get("enabled"):
        lines.append(_cprint(f"[OK] Broadcast: {b['sessions']} sessions, {b['messages']} messages"))
    else:
        lines.append(_cprint("[--] Broadcast: disabled"))

    # Guardrails
    g = results["guardrails"]
    descs = g.get("descriptions", {})
    tag = "[OK]" if g["active"] == g["total"] else "[!!]"
    lines.append(_cprint(f"{tag} Cost guardrails: {g['active']}/{g['total']} active"))
    if g["details"]:
        for name, enabled in g["details"].items():
            marker = "+" if enabled else "-"
            desc = descs.get(name, "")
            lines.append(f"     {marker} {name}: {desc}")

    # MCP
    m = results["mcp"]
    tag = "[OK]" if m["ok"] else "[--]"
    enabled = m.get("enabled", 0)
    disabled = m.get("disabled", 0)
    total = m.get("total", 0)
    fleet_tools = m.get("fleet_tools", 0)
    active_tools = m.get("active_tools", 0)

    header = f"{total} servers"
    if enabled == total:
        header += " (all enabled)"
    elif enabled == 0:
        header += " (all disabled here)"
    else:
        header += f" ({enabled} enabled, {disabled} disabled)"

    if fleet_tools:
        header += f" — {fleet_tools} tools total"
        if active_tools != fleet_tools:
            header += f", {active_tools} active here"
    lines.append(_cprint(f"{tag} MCP: {header}"))

    for sname, sinfo in m.get("servers", {}).items():
        marker = "+" if sinfo["enabled"] else "-"
        tools_str = f" ({sinfo['tools']} tools)" if sinfo.get("tools") is not None else ""
        lines.append(f"     {marker} {sname} [{sinfo['type']}]{tools_str}")

    # Quota
    q = results["quota"]
    tag = "[OK]" if q["ok"] else "[--]"
    lines.append(_cprint(f"{tag} Quota: {q['summary']} | {q['peak']}"))

    # Session (if present)
    s = results.get("session")
    if s:
        lines.append("")
        lines.append(_cprint("[OK] Session metrics:" if s.get("ok") else "[!!] Session metrics:"))
        if "fill_pct" in s:
            fill = s["fill_pct"]
            burn = s.get("burn_rate", 0)
            used = _fmt_bytes(s.get("used", 0))
            remaining = _fmt_bytes(s.get("remaining", 0))
            lines.append(f"     Context: {fill:.0f}% ({used} used, {remaining} remaining)")
            if burn:
                lines.append(f"     Burn rate: {_fmt_bytes(burn)}/turn")
        if "tool_audit" in s:
            audit = s["tool_audit"]
            sorted_tools = sorted(audit.items(), key=lambda x: x[1], reverse=True)[:5]
            lines.append("     Top consumers:")
            for tool, nbytes in sorted_tools:
                lines.append(f"       {tool}: {_fmt_bytes(nbytes)}")
        if "warn_level" in s:
            lines.append(f"     Warning level: {s['warn_level']}")
        if "error" in s:
            lines.append(f"     ({s['error']})")

    return "\n".join(lines)


def format_json(results: dict[str, Any]) -> str:
    return json.dumps(results, indent=2, default=str)


# ── CLI entrypoint ──────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(description="agentihooks status checker")
    parser.add_argument("--json", action="store_true", help="Output JSON instead of colored text")
    parser.add_argument("--session", default=None, help="Session ID for live metrics")
    args = parser.parse_args()

    results = run_all_checks(session_id=args.session)
    if args.json:
        print(format_json(results))
    else:
        print(format_cli(results))


if __name__ == "__main__":
    main()


# ---------------------------------------------------------------------------
# Hook injection probe (P0.4 — agentihooks doctor --debug-hook)
# ---------------------------------------------------------------------------


def _synthetic_payload(event: str) -> dict:
    """Minimal valid payload per Claude Code hook protocol."""
    base = {
        "session_id": "doctor-probe",
        "transcript_path": "",
        "cwd": str(Path.cwd()),
        "hook_event_name": event,
    }
    if event in ("PreToolUse", "PostToolUse"):
        base["tool_name"] = "Bash"
        base["tool_input"] = {"command": "true"}
        base["tool_response"] = {}
    if event == "UserPromptSubmit":
        base["prompt"] = "doctor probe"
    return base


def check_hook_injection() -> dict:
    """Run each hook event with a synthetic payload via `python -m hooks` and
    assert the upstream Claude Code protocol invariants:

      1. exit code is 0 or 2 (never an unexpected non-zero like 1)
      2. stdout is parseable JSON OR empty
      3. if hookSpecificOutput.additionalContext is present, it is a string
         under 10,000 chars (the documented hard cap)

    Returns a dict {ok: bool, events: [...], warnings: [...]}.
    """
    import json as _json
    import os
    import subprocess
    import sys

    repo_root = Path(__file__).resolve().parent.parent
    events = [
        "SessionStart",
        "UserPromptSubmit",
        "PreToolUse",
        "PostToolUse",
        "Stop",
    ]
    results: list[dict] = []
    warnings: list[str] = []

    for event in events:
        payload = _synthetic_payload(event)
        env = {**os.environ, "CLAUDE_HOOK_LOG_ENABLED": "false", "BROADCAST_ENABLED": "false"}
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "hooks"],
                cwd=repo_root,
                input=_json.dumps(payload),
                capture_output=True,
                text=True,
                env=env,
                timeout=30,
            )
        except subprocess.TimeoutExpired:
            results.append({"event": event, "ok": False, "reason": "timeout >30s"})
            warnings.append(f"{event}: hook timed out (>30s)")
            continue

        out = (proc.stdout or "").strip()
        rc = proc.returncode
        ok = True
        reason = None
        ctx_len = 0

        # Per Claude Code hooks protocol:
        #  - SessionStart / UserPromptSubmit: plain-text stdout is valid context.
        #  - PreToolUse / PostToolUse / Stop: prefer hookSpecificOutput JSON;
        #    plain-text stdout is undocumented and may be ignored by some versions.
        accepts_plain_text = event in ("SessionStart", "UserPromptSubmit")

        if rc not in (0, 2):
            ok = False
            reason = f"exit code {rc} (expected 0 or 2)"
        elif out:
            parsed = None
            try:
                parsed = _json.loads(out)
            except _json.JSONDecodeError as e:
                if not accepts_plain_text:
                    ok = False
                    reason = (
                        f"plain-text stdout on {event} is undocumented; emit hookSpecificOutput JSON instead ({e.msg})"
                    )
                else:
                    ctx_len = len(out)
                    if ctx_len >= 10000:
                        ok = False
                        reason = (
                            f"plain-text stdout is {ctx_len} chars; ≥10000 triggers "
                            "Claude Code's tempfile-substitution path"
                        )
            if parsed is not None:
                hso = parsed.get("hookSpecificOutput") or {}
                ac = hso.get("additionalContext")
                if ac is not None:
                    if not isinstance(ac, str):
                        ok = False
                        reason = "additionalContext must be a string"
                    else:
                        ctx_len = len(ac)
                        if ctx_len >= 10000:
                            ok = False
                            reason = (
                                f"additionalContext is {ctx_len} chars; ≥10000 triggers "
                                "Claude Code's tempfile-substitution path (model receives a path, not body)"
                            )

        results.append(
            {
                "event": event,
                "ok": ok,
                "exit_code": rc,
                "stdout_bytes": len(out),
                "additional_context_chars": ctx_len,
                "reason": reason,
                "stderr_first_line": (proc.stderr or "").splitlines()[0] if proc.stderr else "",
            }
        )
        if not ok and reason:
            warnings.append(f"{event}: {reason}")

    return {"ok": all(r["ok"] for r in results), "events": results, "warnings": warnings}


def format_hook_injection(result: dict) -> str:
    lines = ["AgentiHooks doctor — hook injection probe", "=" * 50]
    for ev in result.get("events", []):
        status = "✓" if ev["ok"] else "✗"
        lines.append(
            f"  {status} {ev['event']:<20} exit={ev['exit_code']} "
            f"out_bytes={ev['stdout_bytes']} ctx_chars={ev['additional_context_chars']}"
        )
        if ev.get("reason"):
            lines.append(f"      reason: {ev['reason']}")
        if ev.get("stderr_first_line"):
            lines.append(f"      stderr: {ev['stderr_first_line']}")
    if result.get("warnings"):
        lines.append("")
        lines.append("Warnings:")
        for w in result["warnings"]:
            lines.append(f"  - {w}")
    lines.append("")
    lines.append("Overall: " + ("OK ✓" if result.get("ok") else "FAILED ✗"))
    return "\n".join(lines)
