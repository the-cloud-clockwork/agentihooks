"""A KV v2 secret on OpenBao, merged key by key under check-and-set. Only key names and counts are reported."""

import json
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass

MOUNT = "secret"
CODEX_CREDS = "agentihooks/codex-creds"
URL_ENV = "OPENBAO_URL"
TOKEN_ENV = "OPENBAO_TOKEN"
ATTEMPTS = 3
TIMEOUT_S = 20


class OpenBaoError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(f"OpenBao HTTP {status}: {message}" if status else f"OpenBao unreachable: {message}")
        self.status = status
        self.message = message

    @property
    def conflict(self) -> bool:
        return self.status == 400 and "check-and-set" in self.message


@dataclass(frozen=True)
class OpenBao:
    url: str
    token: str
    opener: Callable = urllib.request.urlopen


def from_env(environ: Mapping[str, str], opener: Callable | None = None) -> OpenBao | None:
    url, token = environ.get(URL_ENV, ""), environ.get(TOKEN_ENV, "")
    if not (url and token):
        return None
    return OpenBao(url, token, opener) if opener else OpenBao(url, token)


def _message(exc: urllib.error.HTTPError) -> str:
    try:
        errors = json.loads(exc.read() or b"{}").get("errors") or []
    except (ValueError, AttributeError):
        errors = []
    return "; ".join(str(error) for error in errors) or str(exc.reason)


def _call(bao: OpenBao, method: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(f"{bao.url.rstrip('/')}/v1/{MOUNT}/data/{path}", data=data, method=method)
    request.add_header("X-Vault-Token", bao.token)
    request.add_header("Content-Type", "application/json")
    try:
        with bao.opener(request, timeout=TIMEOUT_S) as response:
            return json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        raise OpenBaoError(exc.code, _message(exc)) from None
    except (urllib.error.URLError, OSError) as exc:
        raise OpenBaoError(0, str(getattr(exc, "reason", exc))) from None


def read(bao: OpenBao, path: str) -> tuple[dict[str, str], int]:
    try:
        found = _call(bao, "GET", path).get("data") or {}
    except OpenBaoError as exc:
        if exc.status == 404:
            return {}, 0
        raise
    return dict(found.get("data") or {}), int((found.get("metadata") or {}).get("version") or 0)


def merge(bao: OpenBao, path: str, entries: Mapping[str, str], stale: Iterable[str] = ()) -> int:
    """Writes entries over the current keys minus stale ones; returns the total key count."""
    stale = frozenset(stale)
    for attempt in range(ATTEMPTS):
        current, version = read(bao, path)
        merged = {key: value for key, value in current.items() if key not in stale} | dict(entries)
        try:
            _call(bao, "POST", path, {"options": {"cas": version}, "data": merged})
        except OpenBaoError as exc:
            if exc.conflict and attempt < ATTEMPTS - 1:
                continue
            raise
        return len(merged)


def summary(path: str, names: Iterable[str], total: int) -> str:
    return f"OpenBao {MOUNT}/{path}: wrote {', '.join(sorted(names))} ({total} keys total)"
