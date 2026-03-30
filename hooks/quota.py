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
        updated = datetime.fromisoformat(updated_str.rstrip("Z")).replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return {"stale": True}
    if (datetime.now(timezone.utc) - updated).total_seconds() > stale_sec:
        return {"stale": True}
    # Include active account name
    state_file = Path.home() / ".agentihooks" / "state.json"
    if state_file.exists():
        try:
            state = json.loads(state_file.read_text(encoding="utf-8"))
            data["_account"] = state.get("active_quota_account", "default")
        except (json.JSONDecodeError, OSError):
            pass
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
    """Compact quota string — caller applies ANSI color.

    Output: session:50% [1h35m] | all:34% sonnet:5% | extra: €40/100 (40%) resets Apr 1 | balance:€100
    """
    if data.get("stale"):
        return "stale"
    parts = []
    acct = data.get("_account")
    if acct and acct != "default":
        parts.append(f"[{acct}]")

    # Session
    session = data.get("session") or {}
    if (s := session.get("used_pct")) is not None:
        tok = f"session:{s:.0f}%"
        ri = session.get("resets_in_sec")
        if ri is not None:
            tok += f" [{_dur(ri)}]"
        parts.append(tok)

    # Weekly — all models + sonnet
    weekly = data.get("weekly") or {}
    wk_parts = []
    am = weekly.get("all_models")
    if am and (w := am.get("used_pct")) is not None:
        t = f"all:{w:.0f}%"
        if am.get("resets"):
            t += f" resets {am['resets']}"
        wk_parts.append(t)
    sn = weekly.get("sonnet")
    if sn and (w := sn.get("used_pct")) is not None:
        t = f"sonnet:{w:.0f}%"
        if sn.get("resets"):
            t += f" resets {sn['resets']}"
        wk_parts.append(t)
    if wk_parts:
        parts.append(" | ".join(wk_parts))

    # Extra usage / monthly spend
    spend = data.get("monthly_spend")
    if spend and spend.get("amount") is not None:
        sym = {"EUR": "€", "USD": "$", "GBP": "£"}.get(spend.get("currency", ""), "")
        tok = f"extra: {sym}{spend['amount']:.0f}"
        if spend.get("limit"):
            tok += f"/{spend['limit']:.0f}"
        if spend.get("used_pct") is not None:
            tok += f" ({spend['used_pct']:.0f}%)"
        if spend.get("resets"):
            tok += f" resets {spend['resets']}"
        parts.append(tok)

    # Balance
    balance = data.get("balance")
    if balance is not None:
        sym = {"EUR": "€", "USD": "$", "GBP": "£"}.get(
            data.get("monthly_spend", {}).get("currency", ""), ""
        )
        parts.append(f"balance:{sym}{balance}")

    return " | ".join(parts)
