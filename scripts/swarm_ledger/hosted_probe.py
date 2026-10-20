"""The hosted ledger an agent resolves first, kept only while its health check answers."""

import json
import os
import time
from pathlib import Path

HOSTED_ENV = "AGENTIHOOKS_HOSTED_LEDGER_URL"
HOSTED_CREDENTIALS = ("AGENTIHOOKS_LEDGER_AGENT_TOKEN", "AGENTIHOOKS_HIVE_LEDGER_CREDENTIAL")
PROBE_S, PROBE_TTL_S = 2, 30
_answers: dict[str, bool] = {}


def hosted(environ=os.environ) -> str:
    url = address(environ)
    if not url or not any(environ.get(name) for name in HOSTED_CREDENTIALS):
        return ""
    if url not in _answers:
        _answers[url] = answered(url, environ)
    return url if _answers[url] else ""


def address(environ=os.environ) -> str:
    return environ.get(HOSTED_ENV, "").strip().rstrip("/")


def probe_file(environ=os.environ) -> Path:
    return Path(environ.get("AGENTIHOOKS_HOME") or Path.home() / ".agentihooks") / "hosted-ledger-probe.json"


def answered(url: str, environ=os.environ) -> bool:
    from scripts.swarm_ledger.ledger_link import serving

    path, now = probe_file(environ), time.time()
    try:
        cached = json.loads(path.read_text())
    except (OSError, ValueError):
        cached = {}
    cached = cached if isinstance(cached, dict) else {}
    at = cached.get("at")
    if cached.get("url") == url and isinstance(at, int | float) and 0 <= now - at < PROBE_TTL_S:
        return bool(cached.get("answers"))
    answers = serving(PROBE_S, url) is not None
    try:
        path.write_text(json.dumps({"url": url, "at": now, "answers": answers}))
    except OSError:
        pass
    return answers
