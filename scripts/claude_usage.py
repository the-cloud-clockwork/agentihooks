"""Claude quota from one Messages request's rate-limit headers instead of a Claude CLI run."""

import json
import math
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import replace

from scripts.claude_quota_balancer import ProbeResult, QuotaWindow, _error_result, _state

MESSAGES_URL = "https://api.anthropic.com/v1/messages"
TIMEOUT_S = 20
HEADER = "anthropic-ratelimit-unified-"
FIVE_HOUR = "5h"
SEVEN_DAY = "7d"
AUTH_HEADER = "Authorization"
BETA_HEADER = "anthropic-beta"
VERSION_HEADER = "anthropic-version"
TYPE_HEADER = "Content-Type"
OAUTH_BETA = "oauth-2025-04-20"
API_VERSION = "2023-06-01"
JSON_TYPE = "application/json"
SYSTEM = "You are Claude Code, Anthropic's official CLI for Claude."
PROMPT = "OK"
MODELS = {"haiku": "claude-haiku-4-5"}
NO_WINDOWS = "rate-limit windows unavailable"
TIMED_OUT = "probe timed out after {seconds}s"
HTTP_ERROR = "HTTP {status}"
CONTEXT_KEYS = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")


class Unreachable(Exception):
    pass


def _number(raw: object) -> float | None:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _window(headers: Mapping[str, str], name: str) -> QuotaWindow:
    used = _number(headers.get(f"{HEADER}{name}-utilization"))
    reset = _number(headers.get(f"{HEADER}{name}-reset"))
    return QuotaWindow(None if used is None else used * 100, None if reset is None else int(reset))


def result(account: str, headers: Mapping[str, str], body: dict, elapsed_ms: int) -> ProbeResult:
    five_hour, seven_day = _window(headers, FIVE_HOUR), _window(headers, SEVEN_DAY)
    status = str(headers.get(f"{HEADER}status") or "unknown")
    state, margin = _state(status, five_hour, seven_day)
    usage = body.get("usage")
    context = sum(int(usage.get(key) or 0) for key in CONTEXT_KEYS) if isinstance(usage, dict) else None
    found = ProbeResult(
        account=account,
        provider_status=status,
        state=state,
        margin=margin,
        five_hour=five_hour,
        seven_day=seven_day,
        model=str(body.get("model") or "?"),
        context_used=context,
        latency_ms=elapsed_ms,
    )
    return replace(found, error=NO_WINDOWS) if state == "UNKNOWN" else found


def _body(raw: bytes) -> dict:
    try:
        found = json.loads(raw)
    except ValueError:
        return {}
    return found if isinstance(found, dict) else {}


def _request(token: str, model: str) -> urllib.request.Request:
    payload = {
        "model": MODELS.get(model, model),
        "max_tokens": 1,
        "system": SYSTEM,
        "messages": [{"role": "user", "content": PROMPT}],
    }
    headers = {
        AUTH_HEADER: f"Bearer {token}",
        BETA_HEADER: OAUTH_BETA,
        VERSION_HEADER: API_VERSION,
        TYPE_HEADER: JSON_TYPE,
    }
    return urllib.request.Request(MESSAGES_URL, data=json.dumps(payload).encode(), headers=headers)


def probe(account: str, token: str, model: str, opener: Callable | None = None) -> ProbeResult:
    started = time.monotonic()
    try:
        with (opener or urllib.request.urlopen)(_request(token, model), timeout=TIMEOUT_S) as response:
            status, headers, raw = response.status, response.headers, response.read()
    except urllib.error.HTTPError as exc:
        status, headers, raw = exc.code, exc.headers, exc.read()
    except urllib.error.URLError as exc:
        if not isinstance(exc.reason, TimeoutError):
            raise Unreachable from None
        return _error_result(account, TIMED_OUT.format(seconds=TIMEOUT_S))
    except TimeoutError:
        return _error_result(account, TIMED_OUT.format(seconds=TIMEOUT_S))
    except OSError:
        raise Unreachable from None
    elapsed_ms = round((time.monotonic() - started) * 1000)
    if status == 200 or (status == 429 and headers.get(f"{HEADER}status")):
        return result(account, headers, _body(raw), elapsed_ms)
    return _error_result(account, HTTP_ERROR.format(status=status), elapsed_ms)
