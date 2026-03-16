"""Read and parse the Claude.ai console quota JSON file."""
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


def _cfg():
    stale = int(os.getenv("CLAUDE_USAGE_STALE_SEC", "300"))
    path = os.getenv("CLAUDE_USAGE_FILE", "")
    return path, stale


def load_quota() -> Optional[dict]:
    """Return quota dict if fresh, {"stale": True} if outdated, None if disabled/missing."""
    path_str, stale_sec = _cfg()
    if not path_str:
        return None
    path = Path(path_str).expanduser()
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    updated_str = data.get("_updated", "")
    if not updated_str:
        return {"stale": True}
    try:
        updated = datetime.fromisoformat(updated_str.rstrip("Z")).replace(tzinfo=timezone.utc)
    except ValueError:
        return {"stale": True}
    if (datetime.now(timezone.utc) - updated).total_seconds() > stale_sec:
        return {"stale": True}
    return data


def _dur(sec: int) -> str:
    if sec < 60:
        return f"{sec}s"
    m = sec // 60
    if m < 60:
        return f"{m}m"
    h, r = divmod(m, 60)
    return f"{h}h{r:02d}m" if r else f"{h}h"


def fmt_quota(data: dict) -> str:
    """Compact quota string — caller applies ANSI color."""
    if data.get("stale"):
        return "stale"
    parts = []
    session = data.get("session") or {}
    if (s := session.get("used_pct")) is not None:
        parts.append(f"s:{s:.0f}%")
    weekly = data.get("weekly") or {}
    wb = weekly.get("all_models") or (next(iter(weekly.values()), None) if weekly else None)
    if wb and (w := wb.get("used_pct")) is not None:
        parts.append(f"w:{w:.0f}%")
    spend = data.get("monthly_spend")
    if spend and spend.get("amount") is not None and spend.get("limit"):
        sym = {"EUR": "€", "USD": "$", "GBP": "£"}.get(spend.get("currency", ""), "")
        parts.append(f"{sym}{spend['amount']:.0f}/{spend['limit']:.0f}")
    ri = session.get("resets_in_sec")
    if ri is not None and ri < 7200:
        parts.append(f"[{_dur(ri)}]")
    return " ".join(parts)
