"""Codex quota from the ChatGPT usage endpoint: a free read in place of a paid exec probe."""

import contextlib
import json
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path

from hooks import config
from scripts import aicode_tokens
from scripts.claude_quota_balancer import QuotaWindow
from scripts.codex_quota import CodexQuota

USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
USER_AGENT = "codex_cli_rs"
AUTH_HEADER = "Authorization"
ACCOUNT_HEADER = "chatgpt-account-id"
AGENT_HEADER = "User-Agent"
TIMEOUT_S = 20
FIVE_HOUR_SECONDS = 18000
EXPIRED_CODE = "token_expired"
LIMIT_REACHED = "limit_reached"
CACHE_FILE = "codex-usage.json"
CACHE_MODE = 0o600
WINDOWS = ("primary_window", "secondary_window")


def _window(raw: object) -> tuple[str, QuotaWindow] | None:
    if not isinstance(raw, dict) or not isinstance(raw.get("limit_window_seconds"), int | float):
        return None
    name = "five_hour" if raw["limit_window_seconds"] <= FIVE_HOUR_SECONDS else "seven_day"
    used, resets = raw.get("used_percent"), raw.get("reset_at")
    return name, QuotaWindow(None if used is None else float(used), None if resets is None else int(resets))


def parse(body: dict, now: float) -> CodexQuota | None:
    limits = body.get("rate_limit")
    if not isinstance(limits, dict):
        return None
    windows = dict(found for key in WINDOWS if (found := _window(limits.get(key))))
    reached = str(limits.get("rate_limit_reached_type") or (LIMIT_REACHED if limits.get("limit_reached") else ""))
    return CodexQuota(now, str(body.get("plan_type") or "?"), reached=reached, **windows)


def _expired(exc: urllib.error.HTTPError) -> bool:
    try:
        found = json.loads(exc.read())
    except ValueError:
        return False
    if not isinstance(found, dict):
        return False
    nested = found.get("error")
    return (nested if isinstance(nested, dict) else found).get("code") == EXPIRED_CODE


def read(token: str, account_id: str, now: float, opener: Callable | None = None) -> CodexQuota | None:
    headers = {AUTH_HEADER: f"Bearer {token}", ACCOUNT_HEADER: account_id, AGENT_HEADER: USER_AGENT}
    request = urllib.request.Request(USAGE_URL, headers=headers)
    try:
        with (opener or urllib.request.urlopen)(request, timeout=TIMEOUT_S) as response:
            body = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return CodexQuota(now, "?", expired=True) if exc.code == 401 and _expired(exc) else None
    except (OSError, ValueError):
        return None
    try:
        return parse(body, now) if isinstance(body, dict) else None
    except (TypeError, ValueError, KeyError, OverflowError):
        return None


def _cache_path() -> Path:
    return Path(config.AGENTIHOOKS_HOME) / CACHE_FILE


def _reading(item: dict) -> CodexQuota:
    windows = {key: QuotaWindow(**item[key]) for key in ("five_hour", "seven_day")}
    return CodexQuota(
        item["observed_at"], item["plan_type"], reached=item["reached"], expired=item["expired"], **windows
    )


def cached() -> dict[str, CodexQuota]:
    try:
        raw = json.loads(_cache_path().read_text())
        return {name: _reading(item) for name, item in raw.items()}
    except (OSError, ValueError, AttributeError, KeyError, TypeError):
        return {}


def store(name: str, quota: CodexQuota) -> None:
    readings = {key: asdict(value) for key, value in cached().items()} | {name: asdict(quota)}
    with contextlib.suppress(OSError):
        path = _cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        aicode_tokens.atomic_write(path, json.dumps(readings).encode(), CACHE_MODE)
