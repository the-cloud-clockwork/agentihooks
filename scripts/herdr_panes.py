"""Every herdr pane agentihooks opens, one record per terminal, so the sweep closes only those and never a pane the
operator opened."""

import hashlib
import json
import re
import tempfile
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path
from urllib.parse import urlparse

from scripts.herdr_host import Placement, server_socket

ROOT_ENV = "AGENTIHOOKS_HERDR_PANES_DIR"
STORE_ENV = "AGENTIHOOKS_SWARM_REDIS_URL"
WITHHELD = "withheld"


@dataclass(frozen=True)
class PaneRecord:
    pane_id: str
    terminal_id: str
    tab_id: str
    workspace_id: str
    kind: str
    owner_session: str
    launched_at: int
    owner_swarm: str = ""
    swarm_store: str = ""
    route_status: str = ""
    seen: str = ""
    active_at: int = 0
    herdr_server: str = ""
    session_name: str = ""


def root(environ: dict[str, str]) -> Path:
    if environ.get(ROOT_ENV):
        return Path(environ[ROOT_ENV])
    return Path.home() / ".agentihooks" / "herdr" / "panes"


def run_folder(environ: dict[str, str]) -> Path:
    return Path(environ.get("XDG_RUNTIME_DIR", tempfile.gettempdir())) / "agentihooks-claude-terminal"


def _path(record: PaneRecord, environ: dict[str, str]) -> Path:
    server = f"{hashlib.sha256(record.herdr_server.encode()).hexdigest()[:12]}_" if record.herdr_server else ""
    return root(environ) / f"{server}{re.sub(r'[^A-Za-z0-9_-]', '_', record.terminal_id or record.pane_id)}.json"


def store_ref(url: str) -> str:
    parts = urlparse(url)
    if not parts.password:
        return url
    bare = parts._replace(netloc=parts.netloc.rpartition("@")[2]).geturl()
    return f"{WITHHELD}:{hashlib.sha256(bare.encode()).hexdigest()[:16]}"


def store_url(recorded: str, environ: dict[str, str]) -> str | None:
    """A withheld store opens only on this environment's own URL; a legacy bare mark only when that URL withholds."""
    if not recorded.startswith(WITHHELD):
        return recorded
    own = environ.get(STORE_ENV)
    if own and (store_ref(own) == recorded or (recorded == WITHHELD and urlparse(own).password)):
        return own
    return None


def _store(environ: dict[str, str]) -> str:
    return store_ref(environ.get(STORE_ENV, ""))


def _write(record: PaneRecord, environ: dict[str, str]) -> PaneRecord:
    path = _path(record, environ)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(json.dumps(asdict(record)).encode())
    return record


def record(placed: Placement, kind: str, owner_session: str, environ: dict[str, str], now_ms: int) -> PaneRecord:
    swarm = environ.get("AGENTIHOOKS_SWARM", "")
    made = PaneRecord(
        pane_id=placed.pane_id,
        terminal_id=placed.terminal_id,
        tab_id=placed.tab_id,
        workspace_id=placed.workspace_id,
        kind=kind,
        owner_session=owner_session,
        launched_at=now_ms,
        owner_swarm=swarm,
        swarm_store=_store(environ) if swarm else "",
        herdr_server=server_socket(environ),
    )
    return _write(made, environ)


def update(found: PaneRecord, environ: dict[str, str], **changes) -> PaneRecord:
    return _write(replace(found, **changes), environ)


def mark(pane_id: str, environ: dict[str, str], **changes) -> None:
    for found in load(environ) if pane_id else []:
        if found.pane_id == pane_id:
            update(found, environ, **changes)


def forget(found: PaneRecord, environ: dict[str, str]) -> None:
    _path(found, environ).unlink(missing_ok=True)


def _read(path: Path) -> PaneRecord | None:
    try:
        raw = json.loads(path.read_bytes())
        return PaneRecord(**{f.name: raw[f.name] for f in fields(PaneRecord) if f.name in raw})
    except (OSError, ValueError, TypeError):
        return None


def load(environ: dict[str, str]) -> list[PaneRecord]:
    folder = root(environ)
    found = [_read(path) for path in sorted(folder.glob("*.json"))] if folder.is_dir() else []
    server, default = server_socket(environ), server_socket({})
    return [item for item in found if item is not None and (item.herdr_server or default) == server]
