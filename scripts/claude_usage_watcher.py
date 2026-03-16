#!/usr/bin/env python3
"""
Claude.ai usage quota watcher — scrapes claude.ai/settings/usage and writes
a JSON file read by hooks/statusline.py.

Usage:
  agentihooks quota               # open Windows Chrome (headed, default)
  agentihooks quota --headless    # headless Chromium daemon
  agentihooks quota import-cookies
  agentihooks quota status

Or directly:
  python3 scripts/claude_usage_watcher.py [--chromium] [--headless] [--import-cookies]

Browser priority (headed mode):
  1. Windows Chrome  (/mnt/c/Program Files/Google/Chrome/Application/chrome.exe)
  2. Windows Edge    (/mnt/c/.../msedge.exe)
  3. Playwright Chromium (bundled, fallback)

Pass --chromium to force the bundled Chromium regardless.
--headless always uses bundled Chromium (Windows .exe headless is unreliable).

Auth is saved to ~/.agentihooks/playwright_profile/ and reused on every run.
"""
import argparse
import asyncio
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path


# Windows browser candidates (WSL paths), checked in order
_WINDOWS_BROWSERS = [
    "/mnt/c/Program Files/Google/Chrome/Application/chrome.exe",
    "/mnt/c/Program Files (x86)/Google/Chrome/Application/chrome.exe",
    "/mnt/c/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
]


def _find_windows_browser() -> str | None:
    """Return path to Windows Chrome/Edge if accessible from WSL, else None."""
    # Also check user-install Chrome
    try:
        import subprocess
        win_user = subprocess.check_output(
            ["cmd.exe", "/c", "echo %USERNAME%"], stderr=subprocess.DEVNULL
        ).decode().strip()
        user_chrome = f"/mnt/c/Users/{win_user}/AppData/Local/Google/Chrome/Application/chrome.exe"
        candidates = _WINDOWS_BROWSERS + [user_chrome]
    except Exception:
        candidates = _WINDOWS_BROWSERS

    for p in candidates:
        if Path(p).exists():
            return p
    return None


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


def import_cookies(session_key: str) -> None:
    """Write a sessionKey cookie into the Playwright persistent profile."""
    from playwright.sync_api import sync_playwright

    profile_dir = Path.home() / ".agentihooks" / "playwright_profile"
    profile_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            str(profile_dir),
            headless=True,
            args=["--no-sandbox"],
        )
        ctx.add_cookies([{
            "name": "sessionKey",
            "value": session_key,
            "domain": ".claude.ai",
            "path": "/",
            "httpOnly": True,
            "secure": True,
            "sameSite": "Lax",
        }])
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto("https://claude.ai/settings/usage", wait_until="domcontentloaded")
        logged_in = "/login" not in page.url and "/auth" not in page.url
        ctx.close()

    if logged_in:
        print("[quota-watcher] Cookie imported and verified — you are now logged in.", flush=True)
        print("[quota-watcher] Run:  agentihooks quota --headless  (for background daemon)", flush=True)
    else:
        print("[quota-watcher] Cookie imported but still redirected to login — value may be expired.", flush=True)
        sys.exit(1)


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
            result["session"]["resets_at"] = (
                datetime.now(timezone.utc) + timedelta(seconds=sec)
            ).strftime("%Y-%m-%dT%H:%M:%SZ")

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
        amt = float(spend_m2.group(2))
        lim = float(spend_m2.group(4))
        result["monthly_spend"] = {
            "amount": amt, "limit": lim, "currency": cur,
            "used_pct": round(amt / lim * 100, 1) if lim else 0,
        }

    bars = await page.query_selector_all('[role="progressbar"]')
    for bar in bars:
        now_str = await bar.get_attribute("aria-valuenow")
        max_str = await bar.get_attribute("aria-valuemax")
        label_el = await page.evaluate(
            "(el) => { let p = el.parentElement; for(let i=0;i<4;i++){p=p&&p.parentElement;} return p ? p.innerText : ''; }",
            bar,
        )
        if now_str and max_str:
            try:
                pct = float(now_str) / float(max_str) * 100
                label = (label_el or "").lower()
                if "session" in label:
                    result["session"]["used_pct"] = round(pct, 1)
                elif "sonnet" in label:
                    result["weekly"].setdefault("sonnet", {})["used_pct"] = round(pct, 1)
                elif "week" in label or "all" in label:
                    result["weekly"].setdefault("all_models", {})["used_pct"] = round(pct, 1)
            except (ValueError, ZeroDivisionError):
                pass

    return result


async def run(output: Path, poll_sec: int, headless: bool, force_chromium: bool):
    from playwright.async_api import async_playwright

    profile_dir = Path.home() / ".agentihooks" / "playwright_profile"
    profile_dir.mkdir(parents=True, exist_ok=True)

    # Resolve browser executable
    exe: str | None = None
    browser_label = "Chromium (bundled)"
    if not headless and not force_chromium:
        exe = _find_windows_browser()
        if exe:
            browser_label = f"Windows browser: {Path(exe).stem}"
        else:
            print("[quota-watcher] Windows Chrome/Edge not found — falling back to bundled Chromium", flush=True)

    if headless and exe:
        # Windows .exe headless is unreliable; always use bundled Chromium for headless
        exe = None
        browser_label = "Chromium (bundled, headless)"

    print(f"[quota-watcher] browser={browser_label}  output={output}  poll={poll_sec}s", flush=True)

    launch_kwargs: dict = {
        "headless": headless,
        "args": ["--no-sandbox"],
    }
    if exe:
        launch_kwargs["executable_path"] = exe

    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(str(profile_dir), **launch_kwargs)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        while True:
            try:
                data = await scrape(page)
                if not data:
                    print("[quota-watcher] not logged in — log in via the browser window, or run: agentihooks quota import-cookies", flush=True)
                elif data.get("session") or data.get("weekly"):
                    _write_atomic(output, data)
                    s = data.get("session", {})
                    print(f"[quota-watcher] ok  session={s.get('used_pct','?')}%  updated={data['_updated']}", flush=True)
                else:
                    print("[quota-watcher] scraped but found no quota data — page structure may have changed", flush=True)
            except Exception as e:
                print(f"[quota-watcher] error: {e}", flush=True)
            await asyncio.sleep(poll_sec)


def main():
    _load_env()
    ap = argparse.ArgumentParser(
        description="Claude.ai quota watcher",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--output", default=os.getenv("CLAUDE_USAGE_FILE", str(Path.home() / ".agentihooks" / "claude_usage.json")))
    ap.add_argument("--poll", type=int, default=int(os.getenv("CLAUDE_USAGE_POLL_SEC", "60")))
    ap.add_argument("--headless", action="store_true",
                    help="Run headless using bundled Chromium (no window, for background daemon)")
    ap.add_argument("--chromium", action="store_true",
                    help="Force Playwright's bundled Chromium even if Windows Chrome is available")
    ap.add_argument("--import-cookies", action="store_true", dest="import_cookies",
                    help="Paste a sessionKey cookie from browser DevTools — no display needed")
    args = ap.parse_args()

    if args.import_cookies:
        print("How to find the cookie:")
        print("  Chrome/Edge: F12 -> Application -> Cookies -> https://claude.ai -> sessionKey")
        print("  Firefox:     F12 -> Storage -> Cookies -> https://claude.ai -> sessionKey")
        print()
        try:
            value = input("Paste sessionKey value: ").strip()
        except (EOFError, KeyboardInterrupt):
            sys.exit("\nAborted.")
        if not value:
            sys.exit("No value entered.")
        import_cookies(value)
        return

    if not args.output:
        sys.exit("Set CLAUDE_USAGE_FILE or pass --output")
    asyncio.run(run(Path(args.output).expanduser(), args.poll, args.headless, args.chromium))


if __name__ == "__main__":
    main()
