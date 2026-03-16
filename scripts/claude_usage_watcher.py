#!/usr/bin/env python3
"""
Claude.ai usage quota watcher — scrapes claude.ai/settings/usage and writes
a JSON file read by hooks/statusline.py.

Usage:
  agentihooks quota               # Windows Chrome via CDP (default, headed)
  agentihooks quota --headless    # bundled Chromium headless (background daemon)
  agentihooks quota --chromium    # force bundled Chromium headed
  agentihooks quota import-cookies
  agentihooks quota status

Browser strategy:
  Headed (default): launches Windows Chrome/Edge via subprocess + CDP.
    Pipe IPC does not work across WSL boundary — TCP debugging port is used instead.
    Profile stored in ~/.agentihooks/playwright_profile/ (UNC path for chrome.exe).
    Falls back to bundled Chromium if Windows Chrome not found.
  Headless: always uses Playwright's bundled Chromium (Windows .exe headless unreliable).
  --chromium: force bundled Chromium even when Windows Chrome is present.

Auth persists in ~/.agentihooks/playwright_profile/ across all modes.
"""
import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

_CDP_PORT = 9222

_WINDOWS_BROWSERS = [
    "/mnt/c/Program Files/Google/Chrome/Application/chrome.exe",
    "/mnt/c/Program Files (x86)/Google/Chrome/Application/chrome.exe",
    "/mnt/c/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
]


def _find_windows_browser() -> str | None:
    try:
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


def _to_windows_path(linux_path: Path) -> str:
    """Convert a WSL Linux path to a Windows UNC path using wslpath."""
    try:
        result = subprocess.check_output(
            ["wslpath", "-w", str(linux_path)], stderr=subprocess.DEVNULL
        ).decode().strip()
        if result:
            return result
    except Exception:
        pass
    # Fallback: use Windows %LOCALAPPDATA%\agentihooks\playwright_profile
    try:
        local_app = subprocess.check_output(
            ["cmd.exe", "/c", "echo %LOCALAPPDATA%"], stderr=subprocess.DEVNULL
        ).decode().strip()
        return f"{local_app}\\agentihooks\\playwright_profile"
    except Exception:
        return str(linux_path)


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
    """Write a sessionKey cookie using bundled Chromium (headless, no WSL pipe issues)."""
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
        print("[quota-watcher] Cookie imported and verified — you are now logged in.", flush=True)
        print("[quota-watcher] Run:  agentihooks quota --headless  (background daemon)", flush=True)
    else:
        print("[quota-watcher] Still redirected to login — value may be expired.", flush=True)
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


async def run_with_windows_chrome(exe: str, profile_dir: Path, output: Path, poll_sec: int):
    """Launch Windows Chrome/Edge via subprocess + CDP (pipe IPC fails cross-WSL)."""
    from playwright.async_api import async_playwright

    win_profile = _to_windows_path(profile_dir)
    print(f"[quota-watcher] browser={Path(exe).stem}  profile={win_profile}", flush=True)
    print(f"[quota-watcher] output={output}  poll={poll_sec}s", flush=True)

    proc = subprocess.Popen(
        [
            exe,
            f"--remote-debugging-port={_CDP_PORT}",
            f"--user-data-dir={win_profile}",
            "--no-first-run",
            "--no-default-browser-check",
            "--no-service-autorun",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    async with async_playwright() as pw:
        # Wait for Chrome to open the debugging port
        browser = None
        for attempt in range(40):
            try:
                browser = await pw.chromium.connect_over_cdp(f"http://localhost:{_CDP_PORT}")
                break
            except Exception:
                await asyncio.sleep(0.5)

        if browser is None:
            proc.kill()
            raise RuntimeError(f"Chrome did not open CDP port {_CDP_PORT} in time")

        print("[quota-watcher] connected to Chrome via CDP", flush=True)

        contexts = browser.contexts
        ctx = contexts[0] if contexts else await browser.new_context()
        pages = ctx.pages
        page = pages[0] if pages else await ctx.new_page()

        try:
            while True:
                try:
                    data = await scrape(page)
                    if not data:
                        print("[quota-watcher] not logged in — log in via the Chrome window, or run: agentihooks quota import-cookies", flush=True)
                    elif data.get("session") or data.get("weekly"):
                        _write_atomic(output, data)
                        s = data.get("session", {})
                        print(f"[quota-watcher] ok  session={s.get('used_pct','?')}%  updated={data['_updated']}", flush=True)
                    else:
                        print("[quota-watcher] scraped but found no quota data — page structure may have changed", flush=True)
                except Exception as e:
                    print(f"[quota-watcher] error: {e}", flush=True)
                await asyncio.sleep(poll_sec)
        finally:
            try:
                await browser.close()
            except Exception:
                pass
            proc.terminate()


async def run_with_chromium(profile_dir: Path, output: Path, poll_sec: int, headless: bool):
    """Run using Playwright's bundled Chromium (launch_persistent_context)."""
    from playwright.async_api import async_playwright

    print(f"[quota-watcher] browser=Chromium (bundled, headless={headless})  output={output}  poll={poll_sec}s", flush=True)

    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            str(profile_dir),
            headless=headless,
            args=["--no-sandbox"],
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        while True:
            try:
                data = await scrape(page)
                if not data:
                    print("[quota-watcher] not logged in — run: agentihooks quota import-cookies", flush=True)
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
    ap = argparse.ArgumentParser(description="Claude.ai quota watcher")
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

    profile_dir = Path.home() / ".agentihooks" / "playwright_profile"
    profile_dir.mkdir(parents=True, exist_ok=True)
    output = Path(args.output).expanduser()

    # Headed + Windows Chrome = CDP path
    if not args.headless and not args.chromium:
        exe = _find_windows_browser()
        if exe:
            asyncio.run(run_with_windows_chrome(exe, profile_dir, output, args.poll))
            return
        print("[quota-watcher] Windows Chrome/Edge not found — falling back to bundled Chromium", flush=True)

    # Headless or --chromium = bundled Chromium
    asyncio.run(run_with_chromium(profile_dir, output, args.poll, args.headless))


if __name__ == "__main__":
    main()
