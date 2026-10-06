"""Injection trace: what each injecting hook added to a session, recorded when it added it.

One JSON line per directive in ~/.agentihooks/injections/<session>.jsonl, and
operator corrections (a directive wrong for a repository) in
~/.agentihooks/injection_corrections.jsonl.
"""

import json
import re
from datetime import datetime, timezone
from pathlib import Path

_TEXT_MAX = 300
_ENFORCEMENT_LAYER = {"bundle": "bundle", "profile": "profile"}
FILE_LAYERS = ("rule", "doctrine")


def _home() -> Path:
    from hooks import config

    return Path(config.AGENTIHOOKS_HOME)


def _session_path(session_id: str) -> Path:
    return _home() / "injections" / f"{re.sub(r'[^A-Za-z0-9_.-]', '_', session_id)}.jsonl"


def _corrections_path() -> Path:
    return _home() / "injection_corrections.jsonl"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _append(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")


def _read(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


def record(session_id: str, layer: str, source: str, text: str, locator: dict | None = None) -> None:
    if not session_id:
        return
    try:
        from hooks.context.profile_chain import active_profile, read_state

        row = {
            "profile": active_profile(read_state()) or "",
            "at": _now(),
            "layer": layer,
            "source": source,
            "locator": locator or {},
            "text": " ".join(str(text).split())[:_TEXT_MAX],
        }
        _append(_session_path(session_id), row)
    except Exception as e:
        from hooks.common import log

        log("injection trace record failed", {"error": str(e)})


def record_enforcements(session_id: str, entries: list[dict]) -> None:
    for entry in entries:
        layer = _ENFORCEMENT_LAYER.get(entry.get("source", ""), "enforcement")
        locator = {"store": entry.get("store", ""), "id": entry.get("id", "")}
        record(session_id, layer, entry.get("id", ""), entry.get("message") or entry.get("path", ""), locator)


def record_broadcast(session_id: str, msg: dict) -> None:
    if msg.get("source") == "brain-adapter":
        layer, locator = "brain", msg.get("origin") or {}
    else:
        layer, locator = "broadcast", {"id": msg.get("id", "")}
    text = f"From {msg.get('source', 'unknown')}: {msg.get('message', '')}"
    record(session_id, layer, msg.get("id", ""), text, locator)


def record_rows(session_id: str, rows: list[dict]) -> None:
    held = {(row.get("layer"), row.get("source")) for row in trace(session_id)}
    for row in rows:
        if (row.get("layer"), row.get("source")) not in held:
            record(session_id, row["layer"], row["source"], row["text"], row["locator"])


def _manifest(path: Path) -> list[dict]:
    try:
        rows = json.loads(path.read_text())
    except (OSError, ValueError):
        return []
    return rows if isinstance(rows, list) else []


def manifests(environ, target: str) -> list[Path]:
    found = []
    if environ.get("AGENTIHOOKS_PROFILE"):
        found.append(_home() / "profiles" / environ["AGENTIHOOKS_PROFILE"] / f"{target}.sources.json")
    slug, name = environ.get("AGENTIHOOKS_SWARM"), environ.get("AGENTIHOOKS_AGENT_NAME")
    if slug and name:
        found.append(_home() / "swarm" / slug / "prompts" / f"{name}.sources.json")
    return found


def record_session_start(session_id: str, environ, target: str) -> None:
    for path in manifests(environ, target):
        record_rows(session_id, _manifest(path))


def format_locator(locator: dict | None) -> str:
    return " ".join(f"{key}={value}" for key, value in (locator or {}).items())


def trace(session_id: str) -> list[dict]:
    return _read(_session_path(session_id))


def correct(session_id: str, source: str, repo: str, reason: str, quote: str = "") -> dict:
    received = [row for row in trace(session_id) if row.get("source") == source]
    if not received:
        raise ValueError(f"session {session_id} never received a directive from {source}")
    row = {
        "at": _now(),
        "session": session_id,
        "layer": received[-1]["layer"],
        "source": source,
        "locator": received[-1].get("locator") or {},
        "text": received[-1].get("text", ""),
        "repo": repo,
        "reason": reason,
    }
    if quote:
        _check_quote(row, quote)
        row["quote"] = quote
    _append(_corrections_path(), row)
    return row


def _check_quote(row: dict, quote: str) -> None:
    if row["layer"] not in FILE_LAYERS:
        raise ValueError(
            f"--quote names a passage of a rule or doctrine file; {row['source']} is a {row['layer']} directive"
        )
    file = Path(row["locator"]["repo"]) / row["locator"]["path"]
    try:
        text = file.read_text()
    except OSError as e:
        raise ValueError(f"cannot read {file} to find the quoted passage: {e}") from e
    if " ".join(quote.split()) not in " ".join(text.split()):
        raise ValueError(f"the quoted passage is not in {file}")


def corrections() -> list[dict]:
    return _read(_corrections_path())
