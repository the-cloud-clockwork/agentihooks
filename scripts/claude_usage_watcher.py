#!/usr/bin/env python3
"""
Claude.ai usage quota watcher — polls the Claude API for usage data
and writes a JSON file read by hooks/statusline.py.

Usage:
  agentihooks quota               # start daemon (auto-detaches to background)
  agentihooks quota auth          # opens YOUR browser, prompts for cookie
  agentihooks quota import-cookies # paste sessionKey without opening browser
  agentihooks quota status        # show last known quota JSON
  agentihooks quota logs          # tail the daemon log
  agentihooks quota stop          # stop the daemon
  agentihooks quota dump-html     # dump raw API JSON for debugging
"""

import argparse
import asyncio
import json
import os
import platform
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

_STATE_FILE = Path.home() / ".agentihooks" / "claude_auth_state.json"
_LOG_FILE = Path.home() / ".agentihooks" / "logs" / "quota-watcher.log"
_PID_FILE = Path.home() / ".agentihooks" / "quota-watcher.pid"

_BASE_URL = "https://claude.ai"


def _load_env():
    env = Path.home() / ".agentihooks" / ".env"
    if not env.is_file():
        return
    for raw in env.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip().lstrip("export").strip()
        v = v.strip().strip("\"'")
        if k:
            os.environ.setdefault(k, v)


def _write_atomic(path: Path, data: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def _open_browser(url: str) -> None:
    """Open URL in the user's real browser."""
    system = platform.system().lower()
    try:
        if system == "darwin":
            subprocess.Popen(["open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif "microsoft" in platform.release().lower() or Path("/mnt/c/Windows").exists():
            subprocess.Popen(
                ["cmd.exe", "/c", "start", url.replace("&", "^&")],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        else:
            subprocess.Popen(["xdg-open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        print(f"  Could not open browser: {e}", flush=True)
        print(f"  Open manually: {url}", flush=True)


def _is_authenticated() -> bool:
    return _STATE_FILE.exists()


def _daemon_running() -> int | None:
    if not _PID_FILE.exists():
        return None
    try:
        pid = int(_PID_FILE.read_text().strip())
        os.kill(pid, 0)
        return pid
    except (ValueError, ProcessLookupError, PermissionError):
        _PID_FILE.unlink(missing_ok=True)
        return None


def _load_session_key() -> str | None:
    """Extract sessionKey from the saved auth state file."""
    if not _STATE_FILE.exists():
        return None
    try:
        state = json.loads(_STATE_FILE.read_text())
        for c in state.get("cookies", []):
            if c["name"] == "sessionKey":
                return c["value"]
    except (json.JSONDecodeError, KeyError):
        pass
    return None


def _save_session_key(session_key: str) -> None:
    """Save sessionKey to the auth state file (minimal format)."""
    state = {
        "cookies": [
            {
                "name": "sessionKey",
                "value": session_key,
                "domain": ".claude.ai",
                "path": "/",
                "httpOnly": True,
                "secure": True,
                "sameSite": "Lax",
            }
        ],
        "origins": [],
    }
    _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


async def _api_get(session_key: str, path: str) -> dict | None:
    """Make an authenticated GET request to the Claude API."""
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = await browser.new_context(
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        )
        await ctx.add_cookies(
            [
                {
                    "name": "sessionKey",
                    "value": session_key,
                    "domain": ".claude.ai",
                    "path": "/",
                    "httpOnly": True,
                    "secure": True,
                    "sameSite": "Lax",
                }
            ]
        )
        try:
            resp = await ctx.request.get(f"{_BASE_URL}{path}")
            if resp.status == 200:
                ct = resp.headers.get("content-type", "")
                if "json" in ct:
                    return await resp.json()
            return None
        finally:
            await browser.close()


def _format_reset_day(iso_str: str) -> str:
    """Convert ISO timestamp to 'Fri 10:00 AM' style string."""
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return dt.strftime("%a %-I:%M %p").lower()
    except (ValueError, AttributeError):
        return ""


def _map_api_to_schema(api_data: dict) -> dict:
    """Map the /api/organizations/{org}/usage response to our output schema."""
    now = datetime.now(timezone.utc)
    currency = os.getenv("CLAUDE_USAGE_CURRENCY", "EUR")

    result: dict = {
        "_schema_version": 1,
        "_updated": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "_source": "api",
        "session": {},
        "weekly": {},
        "monthly_spend": None,
        "balance": None,
        "extensions": {},
    }

    # Session (5-hour window)
    fh = api_data.get("five_hour")
    if fh and fh.get("utilization") is not None:
        result["session"]["used_pct"] = float(fh["utilization"])
        resets_at = fh.get("resets_at")
        if resets_at:
            try:
                dt = datetime.fromisoformat(resets_at.replace("Z", "+00:00"))
                delta = (dt - now).total_seconds()
                if delta > 0:
                    result["session"]["resets_in_sec"] = int(delta)
                result["session"]["resets_at"] = dt.strftime("%Y-%m-%dT%H:%M:%SZ")
            except (ValueError, AttributeError):
                pass

    # Weekly — all models
    sd = api_data.get("seven_day")
    if sd and sd.get("utilization") is not None:
        entry = {"used_pct": float(sd["utilization"])}
        if sd.get("resets_at"):
            entry["resets"] = _format_reset_day(sd["resets_at"])
        result["weekly"]["all_models"] = entry

    # Weekly — sonnet
    ss = api_data.get("seven_day_sonnet")
    if ss and ss.get("utilization") is not None:
        entry = {"used_pct": float(ss["utilization"])}
        if ss.get("resets_at"):
            entry["resets"] = _format_reset_day(ss["resets_at"])
        result["weekly"]["sonnet"] = entry

    # Extra usage / monthly spend
    eu = api_data.get("extra_usage")
    if eu and eu.get("is_enabled"):
        amt = eu.get("used_credits", 0) / 100
        lim = eu.get("monthly_limit", 0) / 100
        pct = eu.get("utilization", 0)
        result["monthly_spend"] = {
            "amount": round(amt, 2),
            "limit": round(lim, 2),
            "currency": currency,
            "used_pct": round(pct, 1),
            "resets": _get_next_month_first(),
        }

    return result


def _get_next_month_first() -> str:
    """Return 'apr 1' style string for the 1st of next month."""
    now = datetime.now(timezone.utc)
    month = now.month + 1
    year = now.year
    if month > 12:
        month = 1
        year += 1
    from calendar import month_abbr

    return f"{month_abbr[month].lower()} 1"


async def _fetch_usage(session_key: str) -> dict | None:
    """Fetch usage data via API. Returns mapped schema dict or None on failure."""
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = await browser.new_context(
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        )
        await ctx.add_cookies(
            [
                {
                    "name": "sessionKey",
                    "value": session_key,
                    "domain": ".claude.ai",
                    "path": "/",
                    "httpOnly": True,
                    "secure": True,
                    "sameSite": "Lax",
                }
            ]
        )
        try:
            # Get org UUID
            resp = await ctx.request.get(
                f"{_BASE_URL}/api/bootstrap?statsig_hashing_algorithm=djb2&growthbook_format=sdk&include_system_prompts=false"
            )
            if resp.status != 200:
                return None
            bootstrap = await resp.json()
            memberships = bootstrap.get("account", {}).get("memberships", [])
            if not memberships:
                return None
            org_uuid = memberships[0]["organization"]["uuid"]

            # Get usage
            resp = await ctx.request.get(f"{_BASE_URL}/api/organizations/{org_uuid}/usage")
            if resp.status != 200:
                return None
            api_data = await resp.json()
            return _map_api_to_schema(api_data)
        except Exception:
            return None
        finally:
            await browser.close()


def _validate_session_key(session_key: str) -> bool:
    """Check if a sessionKey is valid by calling bootstrap."""

    async def _check():
        from playwright.async_api import async_playwright

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True, args=["--no-sandbox"])
            ctx = await browser.new_context(
                user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            )
            await ctx.add_cookies(
                [
                    {
                        "name": "sessionKey",
                        "value": session_key,
                        "domain": ".claude.ai",
                        "path": "/",
                        "httpOnly": True,
                        "secure": True,
                        "sameSite": "Lax",
                    }
                ]
            )
            try:
                resp = await ctx.request.get(f"{_BASE_URL}/api/bootstrap?statsig_hashing_algorithm=djb2")
                if resp.status != 200:
                    return False
                data = await resp.json()
                return bool(data.get("account", {}).get("memberships"))
            except Exception:
                return False
            finally:
                await browser.close()

    return asyncio.run(_check())


def _import_cookie(session_key: str) -> None:
    """Validate sessionKey via API, save to state file, restart daemon."""
    if _validate_session_key(session_key):
        _save_session_key(session_key)
        print(f"[quota] Auth saved to {_STATE_FILE}", flush=True)
        print("[quota] Authenticated.", flush=True)

        existing = _daemon_running()
        if existing:
            import signal

            os.kill(existing, signal.SIGTERM)
            _PID_FILE.unlink(missing_ok=True)
            print(f"[quota] Restarting daemon (old PID {existing}) with new session...", flush=True)
        else:
            print("[quota] Starting daemon...", flush=True)
        _start_daemon()
    else:
        print("[quota] Cookie rejected — may be expired. Try again.", flush=True)
        sys.exit(1)


def cmd_auth() -> None:
    """Open the real system browser to claude.ai, then prompt for the cookie."""
    print("Opening claude.ai in your browser...", flush=True)
    _open_browser("https://claude.ai")
    print()
    print("Once logged in, copy the session cookie:")
    print("  Chrome/Edge: F12 → Application → Cookies → https://claude.ai → sessionKey")
    print("  Safari:      Develop → Show Web Inspector → Storage → Cookies → sessionKey")
    print()
    try:
        value = input("Paste sessionKey value: ").strip()
    except (EOFError, KeyboardInterrupt):
        sys.exit("\nAborted.")
    if not value:
        sys.exit("No value entered.")
    _import_cookie(value)


async def _run_daemon(output: Path, poll_sec: int):
    """API-based polling loop."""
    while True:
        try:
            sk = _load_session_key()
            if not sk:
                print("[quota] No session key. Run:  agentihooks quota auth", flush=True)
            else:
                data = await _fetch_usage(sk)
                if data is None:
                    print("[quota] API call failed — session may be expired. Run:  agentihooks quota auth", flush=True)
                elif data.get("session") or data.get("weekly"):
                    _write_atomic(output, data)
                    s = data.get("session", {})
                    print(f"[quota] ok  session={s.get('used_pct', '?')}%  updated={data['_updated']}", flush=True)
                else:
                    print("[quota] No quota data in API response.", flush=True)
        except Exception as e:
            print(f"[quota] error: {e}", flush=True)
        await asyncio.sleep(poll_sec)


def _start_daemon(poll: int = 60) -> None:
    """Fork the watcher to the background, log to file, write PID."""
    existing = _daemon_running()
    if existing:
        print(f"[quota] Daemon already running (PID {existing}).", flush=True)
        print(f"  Logs: tail -f {_LOG_FILE}", flush=True)
        return

    _LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

    python = sys.executable
    watcher = str(Path(__file__).resolve())
    output = os.getenv("CLAUDE_USAGE_FILE", str(Path.home() / ".agentihooks" / "claude_usage.json"))

    log_fd = open(_LOG_FILE, "a")
    proc = subprocess.Popen(
        [python, watcher, "--foreground", "--poll", str(poll), "--output", output],
        stdout=log_fd,
        stderr=log_fd,
        start_new_session=True,
    )
    _PID_FILE.write_text(str(proc.pid))
    print(f"[quota] Daemon started (PID {proc.pid}).", flush=True)
    print("  Logs:   agentihooks quota logs", flush=True)
    print("  Status: agentihooks quota status", flush=True)
    print(f"  Stop:   kill {proc.pid}", flush=True)


def cmd_dump() -> None:
    """One-shot: fetch usage via API and print raw JSON."""
    sk = _load_session_key()
    if not sk:
        sys.exit("[quota] Not authenticated. Run: agentihooks quota auth")
    data = asyncio.run(_fetch_usage(sk))
    if data:
        print(json.dumps(data, indent=2))
    else:
        print("[quota] API call failed — session may be expired.", flush=True)


def cmd_stop() -> None:
    """Stop the running daemon."""
    pid = _daemon_running()
    if not pid:
        print("[quota] No daemon running.", flush=True)
        return
    import signal

    os.kill(pid, signal.SIGTERM)
    _PID_FILE.unlink(missing_ok=True)
    print(f"[quota] Daemon stopped (PID {pid}).", flush=True)


def main():
    _load_env()
    ap = argparse.ArgumentParser(description="Claude.ai quota watcher")
    ap.add_argument(
        "--output", default=os.getenv("CLAUDE_USAGE_FILE", str(Path.home() / ".agentihooks" / "claude_usage.json"))
    )
    ap.add_argument("--poll", type=int, default=int(os.getenv("CLAUDE_USAGE_POLL_SEC", "60")))
    ap.add_argument("--foreground", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--auth", action="store_true", help="Open your browser + paste session cookie")
    ap.add_argument("--dump-html", action="store_true", dest="dump_html", help="Dump raw API JSON for debugging")
    ap.add_argument("--import-cookies", action="store_true", dest="import_cookies", help="Paste sessionKey only")
    ap.add_argument(
        "action",
        nargs="?",
        choices=["watch", "auth", "import-cookies", "status", "logs", "stop"],
        help="watch (default) — start background daemon; auth — open browser + paste cookie; "
        "import-cookies — paste only; status — print quota; logs — tail daemon log; "
        "stop — kill daemon; dump-html — dump raw API JSON for debugging",
    )
    args = ap.parse_args()

    if args.auth or args.action == "auth":
        cmd_auth()
        return

    if args.dump_html or args.action == "dump-html":
        cmd_dump()
        return

    if args.import_cookies or args.action == "import-cookies":
        print("Chrome: F12 → Application → Cookies → https://claude.ai → sessionKey")
        print()
        try:
            value = input("Paste sessionKey value: ").strip()
        except (EOFError, KeyboardInterrupt):
            sys.exit("\nAborted.")
        if not value:
            sys.exit("No value entered.")
        _import_cookie(value)
        return

    if args.action == "status":
        out = Path(args.output).expanduser()
        if out.exists():
            print(out.read_text())
        else:
            print("[quota] No data yet.", flush=True)
        return

    if args.action == "logs":
        if _LOG_FILE.exists():
            os.execvp("tail", ["tail", "-f", str(_LOG_FILE)])
        else:
            print("[quota] No log file yet.", flush=True)
        return

    if args.action == "stop":
        cmd_stop()
        return

    if args.foreground:
        asyncio.run(_run_daemon(Path(args.output).expanduser(), args.poll))
        return

    # Default: start the daemon
    if not _is_authenticated():
        print("[quota] Not authenticated yet. Run first:", flush=True)
        print("  agentihooks quota auth", flush=True)
        sys.exit(1)

    _start_daemon(poll=args.poll)


if __name__ == "__main__":
    main()
