import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping

from hooks.context.project_identity import ProjectIdentity, resolve_project
from scripts.claude_config import claude_home

SCOPE_FIELDS = (
    "project_id",
    "project",
    "repo",
    "worktree",
    "cwd",
    "remote",
    "branch",
    "swarm",
    "task",
    "task_revision",
    "lane",
)
PROJECT_FIELDS = ("project_id", "project", "repo", "remote")
FLEET = "fleet"
_UNCLAIMED = ("", "unknown")
_SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


class ScopeRefused(ValueError):
    pass


@dataclass(frozen=True)
class SessionGrant:
    project_ids: frozenset[str]

    def admits(self, project_id: str) -> bool:
        return project_id in _UNCLAIMED or project_id in self.project_ids


def _index_path() -> Path:
    from hooks.config import AGENTIHOOKS_HOME

    return Path(AGENTIHOOKS_HOME) / "brain" / "project-sessions.jsonl"


def _rows() -> dict[str, dict]:
    path = _index_path()
    if not path.exists():
        return {}
    rows = {}
    for line in path.read_text().splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        rows[row["session_id"]] = row
    return rows


def record_session(session_id: str, identity: ProjectIdentity | None) -> None:
    if not session_id:
        return
    if enabled():
        branch = _branch(identity.cwd) if identity else ""
        _observe(session_id, _scope(identity, None, branch, os.environ), datetime.now(timezone.utc).isoformat())
    if not identity:
        return
    from hooks.context.broadcast import _file_lock

    path = _index_path()
    with _file_lock(path):
        row = {"session_id": session_id, **identity.attributes()}
        existing = _rows().get(session_id)
        if existing and all(existing.get(k) == v for k, v in row.items()):
            return
        row["started_at"] = datetime.now(timezone.utc).isoformat()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as stream:
            stream.write(json.dumps(row) + "\n")


def _legacy_identity(session_id: str) -> ProjectIdentity | None:
    if not session_id or "/" in session_id or "\\" in session_id:
        return None
    from hooks.context.broadcast import encode_cwd

    projects = claude_home() / "projects"
    if not projects.is_dir():
        return None
    for folder in projects.iterdir():
        if not (folder / f"{session_id}.jsonl").is_file():
            continue
        candidates = [Path.home()]
        while candidates:
            path = candidates.pop()
            encoded = encode_cwd(str(path))
            if encoded == folder.name:
                return resolve_project(str(path), {})
            if not folder.name.startswith(encoded + "-"):
                continue
            try:
                candidates.extend(
                    child
                    for child in path.iterdir()
                    if child.is_dir() and not child.is_symlink() and folder.name.startswith(encode_cwd(str(child)))
                )
            except OSError:
                continue
    return None


def lookup(session_id: str) -> ProjectIdentity | None:
    row = _rows().get(session_id)
    if row:
        fields = {key: row.get(key, "") for key in ("project", "repo", "worktree", "cwd", "remote")}
        return ProjectIdentity(**fields, project_id=row.get("project_id") or "unknown")
    return _legacy_identity(session_id)


def _scope_path(session_id: str) -> Path | None:
    if not _SESSION_ID.fullmatch(session_id or ""):
        return None
    return _index_path().parent / "session-scopes" / f"{session_id}.jsonl"


def _instant(value: object) -> datetime | None:
    try:
        moment = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _read(path: Path) -> tuple[str, list[dict]]:
    try:
        text = path.read_text(errors="replace")
    except OSError:
        text = ""
    rows = []
    for line in text.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return text, rows


def transitions(session_id: str) -> list[dict]:
    path = _scope_path(session_id)
    return _read(path)[1] if path else []


def _transition_id(session_id: str, at: str, scope: dict) -> str:
    return hashlib.sha256(json.dumps([session_id, at, scope], sort_keys=True).encode()).hexdigest()


def record_scope(
    session_id: str, scope: Mapping[str, object], at: str, grant: SessionGrant | None = None
) -> dict | None:
    values = {name: str(scope.get(name) or "") for name in SCOPE_FIELDS}
    if grant is not None and not grant.admits(values["project_id"]):
        raise ScopeRefused("project is outside the session grant")
    moment = _instant(at)
    if moment is None:
        raise ScopeRefused("transition time is not an ISO 8601 timestamp")
    path = _scope_path(session_id)
    if path is None:
        return None
    from hooks.context.broadcast import _file_lock

    path.parent.mkdir(parents=True, exist_ok=True)
    with _file_lock(path):
        text, rows = _read(path)
        transition_id = _transition_id(session_id, at, values)
        if any(row.get("transition_id") == transition_id for row in rows):
            return None
        if rows and not _changes(rows[-1], values, moment):
            return None
        row = {"session_id": session_id, "sequence": len(rows), "at": at, "transition_id": transition_id, **values}
        with path.open("a") as stream:
            stream.write(("\n" if text and not text.endswith("\n") else "") + json.dumps(row) + "\n")
    return row


def _changes(latest: dict, values: dict, moment: datetime) -> bool:
    if moment < (_instant(latest.get("at")) or moment):
        raise ScopeRefused("transition is older than the latest accepted one")
    revision, accepted = values["task_revision"], str(latest.get("task_revision"))
    same_task = values["task"] and (latest.get("swarm"), latest.get("task")) == (values["swarm"], values["task"])
    if same_task and revision.isdigit() and accepted.isdigit() and int(revision) < int(accepted):
        raise ScopeRefused("task revision is older than the latest accepted one")
    return any(latest.get(name) != value for name, value in values.items())


def _scope_in(rows: list[dict], at: object) -> dict | None:
    moment = _instant(at)
    if moment is None:
        return None
    before = [row for row in rows if (stamp := _instant(row.get("at"))) is not None and stamp <= moment]
    return {name: str(before[-1].get(name) or "") for name in SCOPE_FIELDS} if before else None


def scope_at(session_id: str, at: object) -> dict | None:
    return _scope_in(transitions(session_id), at)


def _attribution(rows: list[dict], event: Mapping, grant: SessionGrant | None) -> dict:
    attrs = event.get("attrs") or {}
    if attrs.get("share") == FLEET:
        return {"attribution": FLEET}
    scope = _scope_in(rows, event.get("at")) or {}
    explicit = {name: str(attrs[name]) for name in PROJECT_FIELDS if attrs.get(name)}
    if explicit.get("project_id", "") not in _UNCLAIMED:
        if grant is not None and not grant.admits(explicit["project_id"]):
            return {"attribution": "refused"}
        scope = {**{name: value for name, value in scope.items() if name not in PROJECT_FIELDS}, **explicit}
        label = "explicit"
    else:
        label = "unknown" if scope.get("project_id", "") in _UNCLAIMED else "scoped"
    return {"attribution": label, **{name: value for name, value in scope.items() if value}}


def enabled(environ: Mapping[str, str] | None = None) -> bool:
    return (os.environ if environ is None else environ).get("AGENTIHOOKS_SESSION_SCOPE") != "0"


def marker_scope(
    session_id: str, marker: Mapping, grant: SessionGrant | None = None, *, replay: bool = False
) -> dict | None:
    if not enabled():
        return None
    attrs = marker.get("attrs") or {}
    if attrs.get("share") == FLEET:
        return {"attribution": FLEET}
    if replay and not marker.get("at"):
        return None
    rows = transitions(session_id)
    if not rows:
        return None
    return _attribution(rows, {"at": marker.get("at"), "attrs": attrs}, grant)


def attribute(session_id: str, events: Iterable[Mapping], grant: SessionGrant | None = None) -> list[dict]:
    rows = transitions(session_id)
    return [_attribution(rows, event, grant) for event in events]


def unattributed_session_events_total(results: Iterable[Mapping]) -> int:
    return sum(result.get("attribution") in ("unknown", "refused") for result in results)


def _branch(cwd: str) -> str:
    from hooks.context.project_identity import _git

    return _git(Path(cwd), "rev-parse", "--abbrev-ref", "HEAD") if cwd else ""


def _scope(identity: ProjectIdentity | None, cwd: str | None, branch: str, env: Mapping[str, str]) -> dict:
    return {
        **(identity.attributes() if identity else {"cwd": cwd}),
        "branch": branch,
        "swarm": env.get("AGENTIHOOKS_SWARM", ""),
        "task": env.get("AGENTIHOOKS_SWARM_TASK", ""),
        "lane": env.get("AGENTIHOOKS_SWARM_LANE", ""),
    }


def _observe(session_id: str, scope: dict, at: str) -> dict | None:
    try:
        return record_scope(session_id, scope, at)
    except (OSError, ValueError):
        return None


def _entry_context(entry: dict) -> tuple[str, object]:
    payload = entry.get("payload") if entry.get("type") == "turn_context" else entry
    cwd = payload.get("cwd") if isinstance(payload, dict) else None
    return str(cwd or ""), entry.get("timestamp")


def observe_transcript(session_id: str, transcript_path: str, environ: Mapping[str, str] | None = None) -> int:
    from hooks.memory.transcript_reader import _load_entries

    env = os.environ if environ is None else environ
    if not enabled(env):
        return 0
    rows = transitions(session_id)
    after = _instant(rows[-1].get("at")) if rows else None
    seen = (rows[-1].get("cwd"), rows[-1].get("branch")) if rows else None
    recorded = 0
    for entry in _load_entries(Path(transcript_path)):
        if not isinstance(entry, dict):
            continue
        cwd, at = _entry_context(entry)
        moment = _instant(at)
        if not cwd or moment is None or (after and moment < after):
            continue
        branch = str(entry.get("gitBranch") or (seen[1] if seen and seen[0] == cwd else ""))
        if (cwd, branch) == seen:
            continue
        seen = (cwd, branch)
        recorded += _observe(session_id, _scope(resolve_project(cwd, env), cwd, branch, env), at) is not None
    return recorded
