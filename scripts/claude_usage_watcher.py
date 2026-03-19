#!/usr/bin/env python3
"""
Claude.ai usage quota watcher — scrapes claude.ai/settings/usage headlessly
and writes a JSON file read by hooks/statusline.py.

Usage:
  agentihooks quota               # start daemon (auto-detaches to background)
  agentihooks quota auth          # opens YOUR browser, prompts for cookie
  agentihooks quota import-cookies # paste sessionKey without opening browser
  agentihooks quota status        # show last known quota JSON
  agentihooks quota logs          # tail the daemon log

Auth is saved to ~/.agentihooks/playwright_profile/ and reused every run.
"""

import argparse
import asyncio
import json
import os
import platform
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

_STATE_FILE = Path.home() / ".agentihooks" / "claude_auth_state.json"
_LOG_FILE = Path.home() / ".agentihooks" / "logs" / "quota-watcher.log"
_PID_FILE = Path.home() / ".agentihooks" / "quota-watcher.pid"


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


def _parse_reset_sec(text: str) -> int | None:
    total = 0
    for val, unit in re.findall(r"(\d+)\s*(h(?:r|our)?s?|m(?:in(?:ute)?s?)?|s(?:ec(?:ond)?s?)?)", text, re.I):
        v = int(val)
        u = unit.lower()[0]
        total += v * (3600 if u == "h" else 60 if u == "m" else 1)
    return total if total else None


def _open_browser(url: str) -> None:
    """Open URL in the user's real browser (Chrome on Windows, default on Mac/Linux)."""
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
    """Check if we have a saved auth state file."""
    return _STATE_FILE.exists()


def _daemon_running() -> int | None:
    """Return PID if the quota daemon is running, else None."""
    if not _PID_FILE.exists():
        return None
    try:
        pid = int(_PID_FILE.read_text().strip())
        os.kill(pid, 0)  # check if alive
        return pid
    except (ValueError, ProcessLookupError, PermissionError):
        _PID_FILE.unlink(missing_ok=True)
        return None


def _import_cookie(session_key: str) -> None:
    """Import sessionKey and save auth state to a JSON file for reuse."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = browser.new_context(
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        )
        ctx.add_cookies(
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
        page = ctx.new_page()
        page.goto("https://claude.ai/settings/usage", wait_until="domcontentloaded")
        try:
            page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass
        logged_in = "/login" not in page.url and "/auth" not in page.url
        if logged_in:
            ctx.storage_state(path=str(_STATE_FILE))
            print(f"[quota] Auth saved to {_STATE_FILE}", flush=True)
        ctx.close()
        browser.close()

    if logged_in:
        print("[quota] Authenticated. Starting daemon...", flush=True)
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


async def scrape(page) -> dict:
    from playwright.async_api import TimeoutError as PwTimeout

    await page.goto("https://claude.ai/settings/usage", wait_until="domcontentloaded")

    if "/login" in page.url or "/auth" in page.url:
        return {}

    try:
        await page.wait_for_load_state("networkidle", timeout=10000)
    except PwTimeout:
        pass

    result: dict = {
        "_schema_version": 1,
        "_updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "_source": "playwright",
        "session": {},
        "weekly": {},
        "monthly_spend": None,
        "balance": None,
        "extensions": {},
    }

    content = await page.content()

    sm = re.search(r"(?:current session|session)[^\n%]{0,80}?(\d+)\s*%", content, re.I)
    if not sm:
        sm = re.search(r"(\d+)\s*%\s*used[^\n]{0,60}(?:session|resets in)", content, re.I)
    if sm:
        result["session"]["used_pct"] = float(sm.group(1))

    rm = re.search(r"resets in\s+([\dhmins ]+)", content, re.I)
    if rm:
        sec = _parse_reset_sec(rm.group(1))
        if sec:
            result["session"]["resets_in_sec"] = sec
            from datetime import timedelta

            result["session"]["resets_at"] = (datetime.now(timezone.utc) + timedelta(seconds=sec)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )

    wm = re.search(r"(?:weekly|all models)[^\n%]{0,120}?(\d+)\s*%", content, re.I)
    if wm:
        result["weekly"]["all_models"] = {"used_pct": float(wm.group(1))}

    wsm = re.search(r"sonnet[^\n%]{0,80}?(\d+)\s*%", content, re.I)
    if wsm:
        result["weekly"]["sonnet"] = {"used_pct": float(wsm.group(1))}

    spend_m2 = re.search(r"([€$£])(\d+(?:\.\d+)?)\s*(?:\n|spent)[^\n]{0,100}?([€$£])(\d+(?:\.\d+)?)", content, re.I)
    if spend_m2:
        sym = spend_m2.group(1)
        cur = {"€": "EUR", "$": "USD", "£": "GBP"}.get(sym, "USD")
        amt, lim = float(spend_m2.group(2)), float(spend_m2.group(4))
        result["monthly_spend"] = {
            "amount": amt,
            "limit": lim,
            "currency": cur,
            "used_pct": round(amt / lim * 100, 1) if lim else 0,
        }

    # Monthly spend limit (text: "Monthly spend limit\n€100" or "€100\nMonthly spend limit")
    limit_m = re.search(r"monthly\s+spend\s+limit\s*([€$£])([\d.]+)", content, re.I)
    if not limit_m:
        limit_m = re.search(r"([€$£])([\d.]+)\s*monthly\s+spend\s+limit", content, re.I)
    if limit_m:
        result["_spend_limit_raw"] = float(limit_m.group(2))

    # Current balance (text: "€99.95\nCurrent balance" or "Current balance\n€99.95")
    bal_m = re.search(r"([€$£])([\d.]+)\s*(?:\n|current)\s*balance", content, re.I)
    if not bal_m:
        bal_m = re.search(r"balance[^\n€$£]{0,30}([€$£])([\d.]+)", content, re.I)
    if bal_m:
        result["balance"] = float(bal_m.group(2))

    bars = await page.query_selector_all('[role="progressbar"]')
    for bar in bars:
        now_str = await bar.get_attribute("aria-valuenow")
        max_str = await bar.get_attribute("aria-valuemax")
        # parent[2] has the label text: "Current session | ... | 50% used"
        label_el = await page.evaluate(
            "(el) => { let p = el.parentElement; for(let i=0;i<2;i++){p=p&&p.parentElement;} return p ? p.innerText : ''; }",
            bar,
        )
        if now_str and max_str:
            try:
                pct = round(float(now_str) / float(max_str) * 100, 1)
                label = (label_el or "").lower()

                # Parse reset time from label text (e.g. "Resets in 1 hr 39 min" or "Resets Fri 10:00 AM")
                reset_match = re.search(r"resets?\s+in\s+([\dhmins ]+)", label, re.I)
                reset_date_match = re.search(r"resets?\s+(\w{3}\s+[\d:]+\s*(?:am|pm)?|\w{3}\s+\d+)", label, re.I)

                if "session" in label:
                    result["session"]["used_pct"] = pct
                    if reset_match:
                        sec = _parse_reset_sec(reset_match.group(1))
                        if sec:
                            result["session"]["resets_in_sec"] = sec
                            from datetime import timedelta

                            result["session"]["resets_at"] = (
                                datetime.now(timezone.utc) + timedelta(seconds=sec)
                            ).strftime("%Y-%m-%dT%H:%M:%SZ")

                elif "sonnet" in label:
                    result["weekly"].setdefault("sonnet", {})["used_pct"] = pct
                    if reset_date_match:
                        result["weekly"]["sonnet"]["resets"] = reset_date_match.group(1).strip()

                elif "all model" in label:
                    result["weekly"].setdefault("all_models", {})["used_pct"] = pct
                    if reset_date_match:
                        result["weekly"]["all_models"]["resets"] = reset_date_match.group(1).strip()

                elif "spent" in label or "€" in label or "$" in label or "£" in label:
                    # Monthly spend bar: "€39.58 spent | ... | 40% used"
                    spend_match = re.search(r"([€$£])([\d.]+)\s*spent", label, re.I)
                    if spend_match:
                        sym = spend_match.group(1)
                        cur = {"€": "EUR", "$": "USD", "£": "GBP"}.get(sym, "USD")
                        amt = float(spend_match.group(2))
                        limit_val = float(max_str) if float(max_str) > 1 else 0
                        # Compute limit from pct: amt / (pct/100) = limit
                        if pct > 0:
                            limit_val = round(amt / (pct / 100), 2)
                        result["monthly_spend"] = {
                            "amount": amt,
                            "limit": limit_val,
                            "currency": cur,
                            "used_pct": pct,
                        }
                        if reset_date_match:
                            result["monthly_spend"]["resets"] = reset_date_match.group(1).strip()
            except (ValueError, ZeroDivisionError):
                pass

    # Use the text-scraped spend limit if we found it (more accurate than calculation)
    if result.get("_spend_limit_raw") and result.get("monthly_spend"):
        result["monthly_spend"]["limit"] = result["_spend_limit_raw"]
    result.pop("_spend_limit_raw", None)

    return result


async def _run_daemon(output: Path, poll_sec: int):
    """Headless scraper loop using saved auth state."""
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = await browser.new_context(
            storage_state=str(_STATE_FILE),
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        )
        page = await ctx.new_page()

        while True:
            try:
                data = await scrape(page)
                if not data:
                    print("[quota] Not logged in. Run:  agentihooks quota auth", flush=True)
                elif data.get("session") or data.get("weekly"):
                    _write_atomic(output, data)
                    s = data.get("session", {})
                    print(f"[quota] ok  session={s.get('used_pct', '?')}%  updated={data['_updated']}", flush=True)
                else:
                    print("[quota] No quota data found — page structure may have changed.", flush=True)
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

    # Resolve paths for the subprocess
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
    ap.add_argument("--import-cookies", action="store_true", dest="import_cookies", help="Paste sessionKey only")
    args = ap.parse_args()

    if args.auth:
        cmd_auth()
        return

    if args.import_cookies:
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

    if args.foreground:
        # Called by _start_daemon — run the actual loop
        asyncio.run(_run_daemon(Path(args.output).expanduser(), args.poll))
        return

    # Default: start the daemon (auto-detaches)
    if not _is_authenticated():
        print("[quota] Not authenticated yet. Run first:", flush=True)
        print("  agentihooks quota auth", flush=True)
        sys.exit(1)

    _start_daemon(poll=args.poll)


if __name__ == "__main__":
    main()
