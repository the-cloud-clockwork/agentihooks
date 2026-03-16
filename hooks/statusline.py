"""StatusLine script for Claude Code's native statusLine setting.

Claude Code pipes a JSON payload to stdin on every turn; this script
reads it, computes context metrics, and prints a multi-line status bar.

Usage (set in settings.json):
    "statusLine": {"type": "command", "command": "cd /app && __PYTHON__ -m hooks.statusline"}

Supports ANSI colors and multiple output lines (each print = a row).
"""

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Load ~/.agentihooks/.env so TOKEN_WARN_PCT etc. are available
import hooks.config  # noqa: F401 — side effect: loads env

# ---------------------------------------------------------------------------
# ANSI helpers
# ---------------------------------------------------------------------------

_RESET = "\033[0m"
_DIM = "\033[2m"
_BOLD = "\033[1m"
_GREEN = "\033[32m"
_YELLOW = "\033[33m"
_RED = "\033[31m"
_CYAN = "\033[36m"
_MAGENTA = "\033[35m"


def _fmt(n: int) -> str:
    """Format token/line count for compact display."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n // 1_000}K"
    return str(n)


def _dur(ms: float) -> str:
    """Format duration from milliseconds to human-readable."""
    secs = ms / 1000
    if secs < 60:
        return f"{secs:.0f}s"
    mins = secs / 60
    if mins < 60:
        return f"{mins:.0f}m"
    hours = mins / 60
    return f"{hours:.1f}h"


def _progress_bar(pct: float, width: int = 15) -> str:
    """Render a visual progress bar with color based on fill level."""
    filled = int(width * pct / 100)
    empty = width - filled

    if pct >= 80:
        color = _RED
    elif pct >= 60:
        color = _YELLOW
    else:
        color = _GREEN

    bar = f"{color}{'█' * filled}{_DIM}{'░' * empty}{_RESET}"
    return bar


def _git_branch() -> str:
    """Get current git branch (fast, cached-friendly)."""
    try:
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            capture_output=True, text=True, timeout=1,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except Exception:
        return ""


def _cache_ratio(current_usage: dict) -> str:
    """Compute cache hit ratio from current_usage tokens."""
    cache_read = current_usage.get("cache_read_input_tokens", 0) or 0
    input_tokens = current_usage.get("input_tokens", 0) or 0
    cache_create = current_usage.get("cache_creation_input_tokens", 0) or 0

    total_input = input_tokens + cache_read + cache_create
    if total_input <= 0:
        return ""

    ratio = cache_read / total_input * 100
    return f"{ratio:.0f}%"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        print("ctx: ?")
        return

    try:
        # ── Extract fields ─────────────────────────────────────────────
        cw = payload.get("context_window") or {}
        cost_data = payload.get("cost") or {}
        model_data = payload.get("model") or {}
        worktree = payload.get("worktree")
        vim = payload.get("vim")

        # Context window
        used_pct = cw.get("used_percentage")
        ctx_size = cw.get("context_window_size")
        total_input = cw.get("total_input_tokens")
        current_usage = cw.get("current_usage") or {}

        if used_pct is None:
            used_pct = 0.0

        # Prefer summing all token types from current_usage — total_input_tokens
        # in the payload only counts *uncached* tokens (often just 1 for cached
        # sessions), making fill% appear 0% despite heavy context usage.
        if current_usage and ctx_size:
            _uncached = current_usage.get("input_tokens", 0) or 0
            _cache_cr = current_usage.get("cache_creation_input_tokens", 0) or 0
            _cache_rd = current_usage.get("cache_read_input_tokens", 0) or 0
            _computed = _uncached + _cache_cr + _cache_rd
            used = _computed if _computed > 0 else (total_input or 0)
            total = ctx_size
            used_pct = used / total * 100 if total else 0.0
        elif total_input and ctx_size:
            used = total_input
            total = ctx_size
            used_pct = used / total * 100
        elif total_input and used_pct > 0:
            total = int(total_input / used_pct * 100)
            used = total_input
        else:
            used = cw.get("used", 0) or 0
            remaining = cw.get("remaining", 0) or 0
            total = used + remaining
            if total > 0:
                used_pct = used / total * 100

        # Model
        model_name = model_data.get("display_name", "")

        # Cost
        cost_usd = cost_data.get("total_cost_usd")
        duration_ms = cost_data.get("total_duration_ms")
        api_ms = cost_data.get("total_api_duration_ms")
        lines_added = cost_data.get("total_lines_added", 0) or 0
        lines_removed = cost_data.get("total_lines_removed", 0) or 0

        # ── Persist metrics to Redis (for burn rate tracking) ──────────
        session_id = payload.get("session_id", "")
        if session_id:
            try:
                from hooks.observability.token_monitor import persist_token_metrics
                import time

                remaining = max(0, (total or 0) - (used or 0))
                from hooks._redis import get_redis

                prev_used = None
                r = get_redis()
                if r is not None:
                    from hooks._redis import redis_key

                    val = r.hget(redis_key("tokens", session_id), "used")
                    if val:
                        prev_used = float(val)

                burn_rate = max(0, used - prev_used) if prev_used is not None else None

                persist_token_metrics(session_id, {
                    "used": used,
                    "remaining": remaining,
                    "fill_pct": round(used_pct, 2),
                    "burn_rate": int(burn_rate) if burn_rate is not None else 0,
                    "last_updated": time.time(),
                })
            except Exception:
                burn_rate = None
        else:
            burn_rate = None

        # ── LINE 1: Context bar + model + cost ─────────────────────────
        bar = _progress_bar(used_pct)
        line1_parts = [f"{bar} {_BOLD}{used_pct:.0f}%{_RESET}"]

        if model_name:
            line1_parts.append(f"{_CYAN}{model_name}{_RESET}")

        if cost_usd is not None:
            line1_parts.append(f"{_GREEN}${cost_usd:.4f}{_RESET}")

        if duration_ms:
            dur_str = _dur(duration_ms)
            line1_parts.append(f"{_DIM}{dur_str}{_RESET}")

        print(" | ".join(line1_parts))

        # ── LINE 2: Details — tokens, lines, cache, git ───────────────
        line2_parts = []

        # Token counts
        line2_parts.append(f"{_DIM}ctx:{_RESET} {_fmt(used)}/{_fmt(total)}")

        # Burn rate (if Redis available)
        if burn_rate is not None and burn_rate > 0:
            line2_parts.append(f"{_DIM}burn:{_RESET} {_fmt(int(burn_rate))}/turn")

        # Lines changed
        if lines_added or lines_removed:
            line2_parts.append(f"{_GREEN}+{lines_added}{_RESET}{_RED}-{lines_removed}{_RESET}")

        # Cache ratio
        cache = _cache_ratio(current_usage)
        if cache:
            line2_parts.append(f"{_DIM}cache:{_RESET} {cache}")

        # API wait ratio
        if api_ms and duration_ms and duration_ms > 0:
            api_pct = api_ms / duration_ms * 100
            line2_parts.append(f"{_DIM}api:{_RESET} {api_pct:.0f}%")

        # Git branch
        branch = _git_branch()
        if branch:
            line2_parts.append(f"{_MAGENTA}{branch}{_RESET}")

        # Worktree indicator
        if worktree:
            wt_name = worktree.get("name", "")
            if wt_name:
                line2_parts.append(f"{_YELLOW}wt:{wt_name}{_RESET}")

        # Vim mode
        if vim:
            mode = vim.get("mode", "")
            if mode:
                color = _GREEN if mode == "NORMAL" else _YELLOW
                line2_parts.append(f"{color}{mode}{_RESET}")

        print(" | ".join(line2_parts))

        # ── LINE 3 (conditional): Threshold warning ────────────────────
        from hooks.config import TOKEN_CONTROL_ENABLED, TOKEN_MONITOR_ENABLED

        if TOKEN_CONTROL_ENABLED and TOKEN_MONITOR_ENABLED and session_id:
            try:
                from hooks.observability.token_monitor import should_warn_context

                warn, level = should_warn_context(float(used_pct), session_id)
                if warn:
                    if level == "critical":
                        print(f"{_RED}{_BOLD}🚨 CONTEXT {used_pct:.0f}% — /compact now or start new session{_RESET}")
                    else:
                        print(f"{_YELLOW}⚠️  CONTEXT {used_pct:.0f}% — consider /compact soon{_RESET}")
            except Exception:
                pass

    except Exception as e:
        print(f"ctx: err ({e})")


if __name__ == "__main__":
    main()
