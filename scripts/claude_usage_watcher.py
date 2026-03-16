#!/usr/bin/env python3
"""
Claude.ai usage quota watcher — scrapes claude.ai/settings/usage and writes
a JSON file read by hooks/statusline.py.

Usage:
  python3 scripts/claude_usage_watcher.py [--output PATH] [--poll SEC] [--headed]

First run: use --headed so you can log in. Auth is saved to
~/.agentihooks/playwright_profile/ and reused on every subsequent run.
"""
import argparse
import asyncio
import json
import os
import re
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
    """Parse 'resets in 3h 14m' → seconds."""
    total = 0
    for val, unit in re.findall(r"(\d+)\s*(h(?:r|our)?s?|m(?:in(?:ute)?s?)?|s(?:ec(?:ond)?s?)?)", text, re.I):
        v = int(val)
        u = unit.lower()[0]
        total += v * (3600 if u == "h" else 60 if u == "m" else 1)
    return total if total else None


async def scrape(page) -> dict:
    from playwright.async_api import TimeoutError as PwTimeout

    await page.goto("https://claude.ai/settings/usage", wait_until="domcontentloaded")

    # Redirect to login?
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

    # Session quota
    sm = re.search(r"(?:current session|session)[^\n%]{0,80}?(\d+)\s*%", content, re.I)
    if not sm:
        sm = re.search(r"(\d+)\s*%\s*used[^\n]{0,60}(?:session|resets in)", content, re.I)
    if sm:
        result["session"]["used_pct"] = float(sm.group(1))

    # Resets in
    rm = re.search(r"resets in\s+([\dhmins ]+)", content, re.I)
    if rm:
        sec = _parse_reset_sec(rm.group(1))
        if sec:
            result["session"]["resets_in_sec"] = sec
            from datetime import timedelta
            result["session"]["resets_at"] = (
                datetime.now(timezone.utc) + timedelta(seconds=sec)
            ).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Weekly all models
    wm = re.search(r"(?:weekly|all models)[^\n%]{0,120}?(\d+)\s*%", content, re.I)
    if wm:
        result["weekly"]["all_models"] = {"used_pct": float(wm.group(1))}

    # Sonnet only
    wsm = re.search(r"sonnet[^\n%]{0,80}?(\d+)\s*%", content, re.I)
    if wsm:
        result["weekly"]["sonnet"] = {"used_pct": float(wsm.group(1))}

    # Monthly spend  (€39.58 ... €100 or $39.58 ... $100)
    spend_m = re.search(
        r"([€$£])(\d+(?:\.\d+)?)\s+(?:spent|used)[^\n]{0,60}?(?:resets|limit)[^\n]{0,60}?(\d+)\s*%\s*used[^\n]{0,40}?[€$£](\d+(?:\.\d+)?)",
        content, re.I | re.S
    )
    if not spend_m:
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
    if spend_m:
        sym = spend_m.group(1)
        cur = {"€": "EUR", "$": "USD", "£": "GBP"}.get(sym, "USD")
        result["monthly_spend"] = {
            "amount": float(spend_m.group(2)),
            "limit": float(spend_m.group(4)),
            "currency": cur,
            "used_pct": float(spend_m.group(3)),
        }

    # Try ARIA progress bars as authoritative source (overrides text scan)
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


async def run(output: Path, poll_sec: int, headed: bool):
    from playwright.async_api import async_playwright

    profile_dir = Path.home() / ".agentihooks" / "playwright_profile"
    profile_dir.mkdir(parents=True, exist_ok=True)

    print(f"[quota-watcher] output → {output}  poll={poll_sec}s", flush=True)

    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            str(profile_dir),
            headless=not headed,
            args=["--no-sandbox"],
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        while True:
            try:
                data = await scrape(page)
                if not data:
                    print("[quota-watcher] not logged in — re-open with --headed to authenticate", flush=True)
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
    ap.add_argument("--headed", action="store_true", help="Run with visible browser (required for first login)")
    args = ap.parse_args()
    if not args.output:
        sys.exit("Set CLAUDE_USAGE_FILE or pass --output")
    asyncio.run(run(Path(args.output).expanduser(), args.poll, args.headed))


if __name__ == "__main__":
    main()
