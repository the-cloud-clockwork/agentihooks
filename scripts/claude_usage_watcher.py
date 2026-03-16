#!/usr/bin/env python3
"""
Claude.ai usage quota watcher — scrapes claude.ai/settings/usage headlessly
and writes a JSON file read by hooks/statusline.py.

Usage:
  agentihooks quota               # headless daemon (polls every 60s)
  agentihooks quota auth           # opens YOUR Chrome/Safari, prompts for cookie
  agentihooks quota import-cookies # paste sessionKey without opening browser
  agentihooks quota status         # show last known quota JSON

Auth flow:
  1. Run `agentihooks quota auth`
  2. Your real browser opens claude.ai (Chrome on Windows, Chrome/Safari on Mac)
  3. Log in if needed
  4. Copy the sessionKey cookie from DevTools (F12 → Application → Cookies)
  5. Paste it when prompted
  6. Done — run `agentihooks quota` for the headless daemon

The cookie is saved to ~/.agentihooks/playwright_profile/ and reused by the
headless Chromium scraper on every run.
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
            # WSL — open in Windows browser
            subprocess.Popen(
                ["cmd.exe", "/c", "start", url.replace("&", "^&")],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        else:
            subprocess.Popen(["xdg-open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        print(f"  Could not open browser: {e}", flush=True)
        print(f"  Open manually: {url}", flush=True)


def _import_cookie(session_key: str) -> None:
    """Write a sessionKey cookie into the Playwright persistent profile (headless)."""
    from playwright.sync_api import sync_playwright

    profile_dir = Path.home() / ".agentihooks" / "playwright_profile"
    profile_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            str(profile_dir), headless=True, args=["--no-sandbox"],
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
        print("[quota-watcher] Authenticated. Start the daemon:", flush=True)
        print("  agentihooks quota", flush=True)
    else:
        print("[quota-watcher] Cookie rejected — may be expired. Try again with a fresh one.", flush=True)
        sys.exit(1)


def cmd_auth() -> None:
    """Open the real system browser to claude.ai, then prompt for the cookie."""
    print("Opening claude.ai in your browser...", flush=True)
    _open_browser("https://claude.ai")
    print()
    print("Once you are logged in:")
    print("  Chrome/Edge: F12 → Application → Cookies → https://claude.ai → sessionKey")
    print("  Safari:      Develop → Show Web Inspector → Storage → Cookies → sessionKey")
    print("  Firefox:     F12 → Storage → Cookies → https://claude.ai → sessionKey")
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
        amt, lim = float(spend_m2.group(2)), float(spend_m2.group(4))
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


async def run(profile_dir: Path, output: Path, poll_sec: int):
    """Headless scraper — no browser window, just polls claude.ai/settings/usage."""
    from playwright.async_api import async_playwright

    print(f"[quota-watcher] headless daemon | output={output} | poll={poll_sec}s", flush=True)

    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            str(profile_dir), headless=True, args=["--no-sandbox"],
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        while True:
            try:
                data = await scrape(page)
                if not data:
                    print("[quota-watcher] Not logged in. Run:  agentihooks quota auth", flush=True)
                elif data.get("session") or data.get("weekly"):
                    _write_atomic(output, data)
                    s = data.get("session", {})
                    print(f"[quota-watcher] ok  session={s.get('used_pct','?')}%  updated={data['_updated']}", flush=True)
                else:
                    print("[quota-watcher] No quota data found — page structure may have changed.", flush=True)
            except Exception as e:
                print(f"[quota-watcher] error: {e}", flush=True)
            await asyncio.sleep(poll_sec)


def main():
    _load_env()
    ap = argparse.ArgumentParser(description="Claude.ai quota watcher")
    ap.add_argument("--output", default=os.getenv("CLAUDE_USAGE_FILE", str(Path.home() / ".agentihooks" / "claude_usage.json")))
    ap.add_argument("--poll", type=int, default=int(os.getenv("CLAUDE_USAGE_POLL_SEC", "60")))
    ap.add_argument("--auth", action="store_true",
                    help="Open your real browser to claude.ai and import the session cookie")
    ap.add_argument("--import-cookies", action="store_true", dest="import_cookies",
                    help="Paste sessionKey without opening a browser")
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

    if not args.output:
        sys.exit("Set CLAUDE_USAGE_FILE or pass --output")

    profile_dir = Path.home() / ".agentihooks" / "playwright_profile"
    profile_dir.mkdir(parents=True, exist_ok=True)
    asyncio.run(run(profile_dir, Path(args.output).expanduser(), args.poll))


if __name__ == "__main__":
    main()
