"""Shared httpx client + helpers for brain-HTTP hooks.

All three brain hooks (brain_adapter, amygdala_hook, brain_writer_hook) used
to read/write the vault via filesystem or SSH. Phase 7 introduces the kernel
kb-router as the single HTTP surface. This module wraps the client logic so
the hooks stay focused on transport-agnostic concerns.

Fallback behavior: if ``BRAIN_URL`` is empty, ``brain_http_enabled`` returns
False and callers keep their legacy code paths. This preserves local/offline
mode and lets the cutover roll incrementally.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin
from urllib.request import Request, urlopen

from hooks.common import log


def brain_http_enabled() -> bool:
    """True when BRAIN_URL is set in the environment."""
    try:
        from hooks.config import BRAIN_URL
    except ImportError:
        return False
    return bool(BRAIN_URL)


def _auth_headers() -> dict[str, str]:
    try:
        from hooks.config import BRAIN_HTTP_TOKEN
    except ImportError:
        return {}
    if not BRAIN_HTTP_TOKEN:
        return {}
    return {"Authorization": f"Bearer {BRAIN_HTTP_TOKEN}"}


def _base_url() -> str:
    from hooks.config import BRAIN_URL

    return BRAIN_URL


def _timeout() -> float:
    from hooks.config import BRAIN_HTTP_TIMEOUT

    return float(BRAIN_HTTP_TIMEOUT)


def _request(
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
    extra_headers: dict[str, str] | None = None,
) -> dict[str, Any] | None:
    """Perform a single HTTP request. Returns parsed JSON or None on failure.

    Failures (timeouts, 5xx) are logged and return None so the caller can
    decide whether to fall back to the filesystem path.
    """
    base = _base_url()
    if not base:
        return None
    qs = f"?{urlencode(params)}" if params else ""
    url = urljoin(base + "/", path.lstrip("/")) + qs
    headers = {"Accept": "application/json"}
    headers.update(_auth_headers())
    if extra_headers:
        headers.update(extra_headers)

    data: bytes | None = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers.setdefault("Content-Type", "application/json")

    req = Request(url, data=data, method=method.upper(), headers=headers)
    try:
        with urlopen(req, timeout=_timeout()) as resp:
            raw = resp.read()
            if not raw:
                return {}
            return json.loads(raw.decode("utf-8"))
    except HTTPError as exc:
        try:
            err_body = exc.read().decode("utf-8", errors="replace")
        except Exception:
            err_body = ""
        log(
            "brain_http: http error",
            {"method": method, "url": url, "status": exc.code, "body": err_body[:256]},
        )
        return None
    except URLError as exc:
        log("brain_http: url error", {"method": method, "url": url, "error": str(exc.reason)})
        return None
    except (json.JSONDecodeError, ValueError) as exc:
        log("brain_http: decode error", {"method": method, "url": url, "error": str(exc)})
        return None
    except Exception as exc:  # noqa: BLE001 — hooks must not raise
        log("brain_http: unexpected error", {"method": method, "url": url, "error": repr(exc)})
        return None


def get(path: str, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
    return _request("GET", path, params=params)


def post(
    path: str,
    body: dict[str, Any] | None = None,
    *,
    idempotency_key: str | None = None,
    params: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    headers = {"X-Idempotency-Key": idempotency_key} if idempotency_key else None
    return _request("POST", path, body=body, params=params, extra_headers=headers)
