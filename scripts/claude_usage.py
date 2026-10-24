"""Claude quota from one Messages request's rate-limit headers instead of a Claude CLI run."""

import http.client
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
BAD_RESPONSE = "bad response from the API"
BAD_TOKEN = "the token is not a valid header value"
REJECTED = "rejected"
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
    windows = [headers.get(f"{HEADER}{name}-status") for name in (FIVE_HOUR, SEVEN_DAY)]
    status = REJECTED if REJECTED in windows else str(headers.get(f"{HEADER}status") or "unknown")
    state, margin = _state(status, five_hour, seven_day)
    found = ProbeResult(
        account=account,
        provider_status=status,
        state=state,
        margin=margin,
        five_hour=five_hour,
        seven_day=seven_day,
        model=str(body.get("model") or "?"),
        context_used=_context(body.get("usage")),
        latency_ms=elapsed_ms,
    )
    return replace(found, error=NO_WINDOWS) if state == "UNKNOWN" else found


def _context(usage: object) -> int | None:
    if not isinstance(usage, dict):
        return None
    counts = [_number(usage.get(key) or 0) for key in CONTEXT_KEYS]
    return None if None in counts else int(sum(counts))


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


def _exchange(
    opener: Callable, request: urllib.request.Request, timeout: float
) -> tuple[int, Mapping[str, str], bytes]:
    try:
        with opener(request, timeout=timeout) as response:
            return response.status, response.headers, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, exc.read()


def probe(
    account: str, token: str, model: str, opener: Callable | None = None, timeout: float = TIMEOUT_S
) -> ProbeResult:
    started = time.monotonic()
    timed_out = TIMED_OUT.format(seconds=f"{timeout:g}")
    try:
        status, headers, raw = _exchange(opener or urllib.request.urlopen, _request(token, model), timeout)
    except urllib.error.URLError as exc:
        if not isinstance(exc.reason, TimeoutError):
            raise Unreachable from None
        return _error_result(account, timed_out)
    except TimeoutError:
        return _error_result(account, timed_out)
    except http.client.HTTPException:
        return _error_result(account, BAD_RESPONSE)
    except ValueError:
        return _error_result(account, BAD_TOKEN)
    except OSError:
        raise Unreachable from None
    elapsed_ms = round((time.monotonic() - started) * 1000)
    if status == 200 or (status == 429 and headers.get(f"{HEADER}status")):
        return result(account, headers, _body(raw), elapsed_ms)
    return _error_result(account, HTTP_ERROR.format(status=status), elapsed_ms)
