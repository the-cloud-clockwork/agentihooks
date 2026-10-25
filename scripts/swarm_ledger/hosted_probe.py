"""The hosted ledger an agent resolves first, kept only while its health check answers."""

import http.client
import json
import os
import time
import urllib.request
from pathlib import Path

HOSTED_ENV = "AGENTIHOOKS_HOSTED_LEDGER_URL"
HOSTED_CREDENTIALS = ("AGENTIHOOKS_LEDGER_AGENT_TOKEN", "AGENTIHOOKS_HIVE_LEDGER_CREDENTIAL")
PROBE_S, PROBE_TTL_S = 2, 30
_answers: dict[str, bool] = {}


def hosted(environ=os.environ) -> str:
    url = address(environ)
    if not url or not any(environ.get(name) for name in HOSTED_CREDENTIALS):
        return ""
    if environ.get("AGENTIHOOKS_HIVE_LEDGER_CREDENTIAL"):
        return url
    if url not in _answers:
        _answers[url] = answered(url, environ)
    return url if _answers[url] else ""


def address(environ=os.environ) -> str:
    return environ.get(HOSTED_ENV, "").strip().rstrip("/")


def probe_file(environ=os.environ) -> Path:
    return Path(environ.get("AGENTIHOOKS_HOME") or Path.home() / ".agentihooks") / "hosted-ledger-probe.json"


def healthy(url: str) -> bool:
    from scripts.swarm_ledger import ledger_link

    try:
        with ledger_link.urlopen(urllib.request.Request(f"{url}/healthz"), PROBE_S) as response:
            return isinstance(json.loads(response.read()), dict)
    except (OSError, ValueError, http.client.HTTPException):
        return False


def cached_answer(path: Path) -> dict:
    try:
        cached = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return cached if isinstance(cached, dict) else {}


def answered(url: str, environ=os.environ) -> bool:
    path, now = probe_file(environ), time.time()
    cached = cached_answer(path)
    at = cached.get("at")
    if cached.get("url") == url and isinstance(at, int | float) and 0 <= now - at < PROBE_TTL_S:
        return bool(cached.get("answers"))
    answers = healthy(url)
    try:
        path.write_text(json.dumps({"url": url, "at": now, "answers": answers}))
    except OSError:
        pass
    return answers
