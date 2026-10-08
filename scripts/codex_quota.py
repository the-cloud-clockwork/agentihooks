import json
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from scripts.claude_quota_balancer import QuotaWindow, _state

FIVE_HOUR_MINUTES = 300
TAIL_BYTES = 512 * 1024
RECENT_ROLLOUTS = 8
SESSION_ID_LENGTH = 36


@dataclass(frozen=True)
class CodexQuota:
    observed_at: float
    plan_type: str
    five_hour: QuotaWindow = field(default_factory=QuotaWindow)
    seven_day: QuotaWindow = field(default_factory=QuotaWindow)

    @property
    def state(self) -> str:
        windows = [w for w in (self.five_hour, self.seven_day) if w.used is not None]
        if not windows:
            return "UNKNOWN"
        worst = max(windows, key=lambda w: w.used)
        return _state("allowed", worst, worst)[0]

    @property
    def highest_used(self) -> float | None:
        used = [w.used for w in (self.five_hour, self.seven_day) if w.used is not None]
        return max(used) if used else None


def codex_home(environ: dict[str, str]) -> Path:
    value = environ.get("CODEX_HOME", "").split(",")[0].strip()
    return Path(value).expanduser() if value else Path(environ.get("HOME", str(Path.home()))) / ".codex"


def _window(raw: object) -> tuple[str, QuotaWindow] | None:
    if not isinstance(raw, dict) or raw.get("used_percent") is None:
        return None
    minutes = raw.get("window_minutes") or 0
    name = "five_hour" if minutes <= FIVE_HOUR_MINUTES else "seven_day"
    resets = raw.get("resets_at")
    return name, QuotaWindow(used=float(raw["used_percent"]), resets_at=int(resets) if resets else None)


def parse_event(line: str) -> CodexQuota | None:
    try:
        event = json.loads(line)
    except ValueError:
        return None
    payload = event.get("payload") if isinstance(event, dict) else None
    limits = payload.get("rate_limits") if isinstance(payload, dict) else None
    if not isinstance(limits, dict):
        return None
    windows = dict(w for w in (_window(limits.get("primary")), _window(limits.get("secondary"))) if w)
    if not windows:
        return None
    try:
        observed = datetime.fromisoformat(str(event.get("timestamp", "")).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None
    return CodexQuota(observed_at=observed, plan_type=str(limits.get("plan_type") or "?"), **windows)


def _last_in(path: Path) -> CodexQuota | None:
    try:
        with path.open("rb") as handle:
            handle.seek(max(0, path.stat().st_size - TAIL_BYTES))
            lines = handle.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        if '"rate_limits"' in line and (quota := parse_event(line)) is not None:
            return quota
    return None


def rollout_session_id(path: Path) -> str:
    return path.stem[-SESSION_ID_LENGTH:]


def latest_codex_quota(
    environ: dict[str, str] | None = None, keep: Callable[[str], bool] | None = None
) -> CodexQuota | None:
    """Newest rate-limit event among the recent rollouts whose session id ``keep`` accepts."""
    sessions = codex_home(dict(os.environ if environ is None else environ)) / "sessions"
    try:
        rollouts = sorted(sessions.glob("*/*/*/rollout-*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return None
    if keep is not None:
        rollouts = [path for path in rollouts if keep(rollout_session_id(path))]
    found = [q for path in rollouts[:RECENT_ROLLOUTS] if (q := _last_in(path)) is not None]
    return max(found, key=lambda q: q.observed_at, default=None)


def session_quota(environ: dict[str, str], session_id: str) -> CodexQuota | None:
    found = sorted((codex_home(environ) / "sessions").glob(f"*/*/*/rollout-*{session_id}.jsonl"))
    return _last_in(found[-1]) if found else None
