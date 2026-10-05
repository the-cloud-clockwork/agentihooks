import json
from datetime import datetime, timezone
from pathlib import Path

from hooks.context.project_identity import ProjectIdentity, resolve_project


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
    if not session_id or not identity:
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

    projects = Path.home() / ".claude" / "projects"
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
        return ProjectIdentity(**{key: row.get(key, "") for key in ("project", "repo", "worktree", "cwd", "remote")})
    return _legacy_identity(session_id)
