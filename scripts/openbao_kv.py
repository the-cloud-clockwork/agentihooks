import json
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field

MOUNT = "secret"
CODEX_CREDS = "agentihooks/codex-creds"
CLAUDE_CREDS = "agentihooks/claude-creds"
URL_ENV = "OPENBAO_URL"
TOKEN_ENV = "OPENBAO_TOKEN"
CAS_RETRIES = 3
TIMEOUT_S = 20
NOT_AN_OBJECT = "response is not a JSON object"


class OpenBaoError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(f"OpenBao HTTP {status}: {message}" if status else f"OpenBao request failed: {message}")
        self.status = status
        self.message = message

    @property
    def conflict(self) -> bool:
        return self.status == 400 and "check-and-set" in self.message


@dataclass(frozen=True)
class OpenBao:
    url: str
    token: str = field(repr=False)
    opener: Callable | None = None


def from_env(environ: Mapping[str, str], opener: Callable | None = None) -> OpenBao | None:
    url, token = environ.get(URL_ENV), environ.get(TOKEN_ENV)
    return OpenBao(url, token, opener) if url and token else None


def _message(exc: urllib.error.HTTPError) -> str:
    try:
        errors = json.loads(exc.read()).get("errors") or []
    except (ValueError, AttributeError):
        errors = []
    return "; ".join(str(error) for error in errors) or str(exc.reason)


def _call(bao: OpenBao, path: str, body: dict | None = None) -> dict:
    data = None if body is None else json.dumps(body).encode()
    opener = bao.opener or urllib.request.urlopen
    try:
        headers = {"X-Vault-Token": bao.token, "Content-Type": "application/json"}
        request = urllib.request.Request(f"{bao.url.rstrip('/')}/v1/{MOUNT}/data/{path}", data=data, headers=headers)
        with opener(request, timeout=TIMEOUT_S) as response:
            raw = response.read()
        found = json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raise OpenBaoError(exc.code, _message(exc)) from None
    except urllib.error.URLError as exc:
        raise OpenBaoError(0, str(exc.reason)) from None
    except (OSError, ValueError) as exc:
        raise OpenBaoError(0, str(exc)) from None
    if not isinstance(found, dict):
        raise OpenBaoError(0, NOT_AN_OBJECT)
    return found


def read(bao: OpenBao, path: str) -> tuple[dict[str, str], int]:
    try:
        found = _call(bao, path).get("data") or {}
    except OpenBaoError as exc:
        if exc.status == 404:
            return {}, 0
        raise
    return dict(found.get("data") or {}), int((found.get("metadata") or {}).get("version") or 0)


def merge(bao: OpenBao, path: str, entries: Mapping[str, str], stale: Iterable[str] = ()) -> int:
    stale = frozenset(stale)
    for _ in range(CAS_RETRIES + 1):
        current, version = read(bao, path)
        merged = {key: value for key, value in current.items() if key not in stale} | dict(entries)
        try:
            _call(bao, path, {"options": {"cas": version}, "data": merged})
        except OpenBaoError as exc:
            if not exc.conflict:
                raise
            conflict = exc
            continue
        return len(merged)
    raise conflict


def summary(path: str, names: Iterable[str], total: int) -> str:
    return f"OpenBao {MOUNT}/{path}: wrote {', '.join(sorted(names))} ({total} keys total)"
