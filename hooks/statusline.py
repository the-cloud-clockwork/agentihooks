"""StatusLine script for Claude Code's native statusLine setting.

Claude Code pipes a JSON payload to stdin on every turn; this script
reads it, computes context metrics, and prints a multi-line status bar.

Usage (set in settings.json):
    "statusLine": {"type": "command", "command": "cd /app && __PYTHON__ -m hooks.statusline"}

Supports ANSI colors and multiple output lines (each print = a row).

Native fields used from Claude Code's statusline JSON:
  - context_window.used_percentage, context_window_size, current_usage
  - cost.total_cost_usd, total_duration_ms, total_api_duration_ms
  - rate_limits.five_hour.used_percentage/resets_at
  - rate_limits.seven_day.used_percentage/resets_at
  - model.display_name, vim.mode, worktree.*
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

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


def _pct_color(pct: float) -> str:
    """Return ANSI color code based on percentage threshold."""
    if pct >= 80:
        return _RED
    if pct >= 60:
        return _YELLOW
    return _GREEN


def _progress_bar(pct: float, width: int = 15) -> str:
    """Render a visual progress bar with color based on fill level."""
    filled = int(width * pct / 100)
    empty = width - filled
    return f"{_pct_color(pct)}{'█' * filled}{_DIM}{'░' * empty}{_RESET}"


def _git_branch() -> str:
    """Get current git branch (fast, cached-friendly)."""
    try:
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            capture_output=True,
            text=True,
            timeout=1,
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


def _fmt_resets_at(epoch: float) -> str:
    """Format a reset epoch timestamp as relative time or short date."""
    now = datetime.now(timezone.utc)
    reset = datetime.fromtimestamp(epoch, tz=timezone.utc)
    delta = reset - now
    secs = int(delta.total_seconds())
    if secs <= 0:
        return "now"
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        h = secs // 3600
        m = (secs % 3600) // 60
        return f"{h}h{m:02d}m" if m else f"{h}h"
    return reset.strftime("%a %H:%M").lower()


def _rate_limit_str(rate_limits: dict) -> str:
    """Format native rate_limits into a compact colored string.

    Maps Claude Code's JSON keys to descriptive labels matching the /config UI:
      five_hour  → "session"   (Current session window)
      seven_day  → "weekly"    (Current week, all models)
    """
    parts = []

    for key, label in [("five_hour", "session"), ("seven_day", "weekly")]:
        window = rate_limits.get(key)
        if not window:
            continue
        pct = window.get("used_percentage")
        if pct is None:
            continue

        part = f"{label}:{_pct_color(pct)}{pct:.0f}%{_RESET}"

        resets_at = window.get("resets_at")
        if resets_at:
            part += f" {_DIM}[{_fmt_resets_at(resets_at)}]{_RESET}"

        parts.append(part)

    return " | ".join(parts)


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
        rate_limits = payload.get("rate_limits") or {}

        # Context window — trust Claude Code's native used_percentage
        used_pct = cw.get("used_percentage") or 0.0
        ctx_size = cw.get("context_window_size") or 0
        current_usage = cw.get("current_usage") or {}
        used = int(ctx_size * used_pct / 100) if ctx_size else 0
        total = ctx_size

        # Model
        model_name = model_data.get("display_name", "")

        # Cost
        cost_usd = cost_data.get("total_cost_usd")
        duration_ms = cost_data.get("total_duration_ms")
        api_ms = cost_data.get("total_api_duration_ms")
        lines_added = cost_data.get("total_lines_added", 0) or 0
        lines_removed = cost_data.get("total_lines_removed", 0) or 0

        # ── LINE 1: Context bar + model + cost ─────────────────────────
        bar = _progress_bar(used_pct)
        line1_parts = [f"{bar} {_BOLD}{used_pct:.0f}%{_RESET}"]

        if model_name:
            line1_parts.append(f"{_CYAN}{model_name}{_RESET}")

        if cost_usd is not None:
            line1_parts.append(f"{_GREEN}${cost_usd:.4f}{_RESET}")

        if duration_ms:
            line1_parts.append(f"{_DIM}{_dur(duration_ms)}{_RESET}")

        print(" | ".join(line1_parts))

        # ── LINE 2: Details — tokens, lines, cache, git ───────────────
        line2_parts = []

        # Token counts
        if total:
            line2_parts.append(f"{_DIM}ctx:{_RESET} {_fmt(used)}/{_fmt(total)}")

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

        if line2_parts:
            print(" | ".join(line2_parts))

        # ── LINE 2b: AgentiHooks — always shown ──────────────────────
        # Format: ah: profile | sp:none | ovl:none | ch:none
        try:
            import json as _json_ah

            from hooks.config import AGENTIHOOKS_HOME

            # --- Global: profile + settings-profile from state.json ---
            _g_profile = "none"
            _g_sp = "none"
            _state_path = Path(AGENTIHOOKS_HOME) / "state.json"
            if _state_path.exists():
                _ah_state = _json_ah.loads(_state_path.read_text())
                _g_profile = _ah_state.get("targets", {}).get("global", {}).get("profile", "") or "none"
                _g_sp = _ah_state.get("targets", {}).get("global", {}).get("settings_profile", "") or "none"

            # --- Local: per-repo override from .agentihooks.json ---
            _l_profile = ""
            _l_channels: list[str] = []
            cwd = os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd())
            _cfg_path = Path(cwd) / ".agentihooks.json"
            if _cfg_path.exists():
                _cfg = _json_ah.loads(_cfg_path.read_text())
                _l_profile = _cfg.get("profile", "")
                _l_channels = _cfg.get("channels", []) or []

            # --- Overlay ---
            _ovl_str = "none"
            try:
                # scripts/ is a sibling of hooks/ — ensure repo root is on path
                _repo_root = str(Path(__file__).resolve().parent.parent)
                if _repo_root not in sys.path:
                    sys.path.insert(0, _repo_root)
                from scripts.overlay import get_active_overlays

                _overlays = get_active_overlays()
                if _overlays:
                    _ovl_str = ",".join(o.get("name", "?") for o in _overlays)
            except Exception:
                pass

            # --- Build the display ---
            # Profile: show local override if different from global
            if _l_profile and _l_profile != _g_profile:
                _prof_display = f"{_MAGENTA}{_l_profile}{_RESET} {_DIM}({_g_profile}){_RESET}"
            else:
                _prof_display = f"{_MAGENTA}{_g_profile}{_RESET}"

            # Settings-profile — show profile name as default when no explicit override
            _sp_value = _g_sp if _g_sp != "none" else _g_profile
            _sp_color = _DIM if _g_sp == "none" else ""
            _sp_display = f"{_sp_color}{_sp_value}{_RESET}"

            # Overlay
            _ovl_color = _CYAN if _ovl_str != "none" else _DIM
            _ovl_display = f"{_ovl_color}{_ovl_str}{_RESET}"

            # Channels
            _ch_str = ",".join(_l_channels) if _l_channels else "none"
            _ch_color = "" if _l_channels else _DIM
            _ch_display = f"{_ch_color}{_ch_str}{_RESET}"

            print(
                f"  {_DIM}agentihooks:{_RESET} {_prof_display}"
                f"  {_DIM}settings:{_RESET}{_sp_display}"
                f"  {_DIM}overlay:{_RESET}{_ovl_display}"
                f"  {_DIM}channels:{_RESET}{_ch_display}"
            )
        except Exception:
            pass

        # ── LINE 3: Rate limits (native) + context warning ────────────
        parts_3 = []

        # Native rate limits from Claude Code
        rl_str = _rate_limit_str(rate_limits)
        if rl_str:
            parts_3.append(rl_str)

        # Context threshold warning (compact advisor)
        from hooks.config import TOKEN_CONTROL_ENABLED, TOKEN_MONITOR_ENABLED

        session_id = payload.get("session_id", "")
        if TOKEN_CONTROL_ENABLED and TOKEN_MONITOR_ENABLED and session_id:
            try:
                from hooks.observability.token_monitor import should_warn_context

                warn, level = should_warn_context(float(used_pct), session_id)
                if warn:
                    try:
                        from hooks.config import COMPACT_SUGGEST_ENABLED

                        if COMPACT_SUGGEST_ENABLED:
                            from hooks.context.compact_advisor import format_suggestion
                            from hooks.observability.context_audit import get_audit_summary

                            audit = get_audit_summary(session_id)
                            suggestion = format_suggestion(float(used_pct), level, audit if audit else None)
                            color = _RED + _BOLD if level == "critical" else _YELLOW
                            parts_3.append(f"{color}{suggestion}{_RESET}")
                        else:
                            raise ImportError("disabled")
                    except Exception:
                        if level == "critical":
                            parts_3.append(
                                f"{_RED}{_BOLD}CONTEXT {used_pct:.0f}% — /compact now or start new session{_RESET}"
                            )
                        else:
                            parts_3.append(f"{_YELLOW}CONTEXT {used_pct:.0f}% — consider /compact soon{_RESET}")
            except Exception:
                pass

        # Peak/off-peak indicator — uses native session rate limit for context
        try:
            from hooks.config import PEAK_HOURS_ENABLED, PEAK_HOURS_END, PEAK_HOURS_START, PEAK_HOURS_TZ

            if PEAK_HOURS_ENABLED:
                from hooks.observability.peak_hours import is_peak_now, to_local_hour

                local_start, local_abbrev = to_local_hour(PEAK_HOURS_START, PEAK_HOURS_TZ)
                local_end, _ = to_local_hour(PEAK_HOURS_END, PEAK_HOURS_TZ)
                if is_peak_now(PEAK_HOURS_START, PEAK_HOURS_END, PEAK_HOURS_TZ):
                    five_h = rate_limits.get("five_hour", {})
                    session_pct = five_h.get("used_percentage", 0) or 0
                    if session_pct > 50:
                        parts_3.append(
                            f"{_YELLOW}PEAK — sessions burn faster until {local_end:02d}:00 {local_abbrev}{_RESET}"
                        )
                    else:
                        parts_3.append(
                            f"{_YELLOW}PEAK {local_start:02d}:00-{local_end:02d}:00 {local_abbrev}{_RESET}"
                        )
                else:
                    parts_3.append(f"{_GREEN}OFF-PEAK — full session rate{_RESET}")
        except Exception:
            pass

        if parts_3:
            print(f"  {_DIM}|{_RESET}  ".join(parts_3))

    except Exception as e:
        print(f"ctx: err ({e})")


if __name__ == "__main__":
    main()
