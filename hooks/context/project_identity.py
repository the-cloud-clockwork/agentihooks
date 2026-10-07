import json
import os
import re
import subprocess
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Mapping


@dataclass(frozen=True)
class ProjectIdentity:
    project: str
    repo: str
    worktree: str = ""
    cwd: str = ""
    remote: str = ""
    project_id: str = "unknown"

    @property
    def project_identity_ambiguities_total(self) -> int:
        return int(self.project_id == "unknown")

    def attributes(self) -> dict[str, str]:
        return asdict(self)


def _git(cwd: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def canonical_remote(remote: str) -> str:
    prefix = r"(?:https://|ssh://(?:git@)?|git@)"
    match = re.fullmatch(
        prefix + r"([A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?)([/:])([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+)/?", remote
    )
    if not match:
        return "unknown"
    host, separator, owner, repo = match.groups()
    if "." not in host or ".." in host or separator != (":" if remote.startswith("git@") else "/"):
        return "unknown"
    repo = repo.removesuffix(".git")
    if owner in (".", "..") or repo in ("", ".", ".."):
        return "unknown"
    project_id = f"{host.lower()}/{owner}/{repo}"
    return project_id.lower() if host.lower() == "github.com" else project_id


def _folder_identity(cwd: str) -> ProjectIdentity | None:
    path = Path(cwd).expanduser().resolve()
    common = _git(path, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if common:
        root = Path(common).parent
        top = _git(path, "rev-parse", "--show-toplevel")
        project_id = canonical_remote(_git(path, "remote", "get-url", "origin"))
        slug = project_id.split("/", 1)[1] if project_id != "unknown" else ""
        return ProjectIdentity(
            root.name, root.name, Path(top).name if top != str(root) else "", str(path), slug, project_id
        )
    scratch = Path.home() / "scratchpad"
    if path.is_relative_to(scratch) and len(path.relative_to(scratch).parts) >= 2:
        project = path.relative_to(scratch).parts[0]
        return ProjectIdentity(project, project, cwd=str(path))
    return None


def _same_checkout(working: ProjectIdentity, repo: str) -> bool:
    common = _git(Path(repo), "rev-parse", "--path-format=absolute", "--git-common-dir")
    if common:
        return common == _git(Path(working.cwd), "rev-parse", "--path-format=absolute", "--git-common-dir")
    return Path(working.cwd).resolve().is_relative_to(Path(repo).resolve())


def _project_aliases(document: Mapping[str, object] | None) -> Mapping[str, str]:
    if document is None:
        return {}
    if document.get("schema_version") != "2.0" or set(document) != {"schema_version", "aliases"}:
        raise ValueError("Invalid project alias map")
    aliases = document["aliases"]
    if not isinstance(aliases, dict):
        raise ValueError("Invalid project alias map")
    for source, target in aliases.items():
        if not isinstance(source, str) or not isinstance(target, str):
            raise ValueError("Invalid project alias map")
        if (
            "unknown" in (source, target)
            or canonical_remote(f"https://{source}") != source
            or canonical_remote(f"https://{target}") != target
        ):
            raise ValueError("Invalid project alias map")
    for source in aliases:
        _aliased_project(source, aliases)
    return aliases


def _aliased_project(project_id: str, aliases: Mapping[str, str]) -> str:
    seen = set()
    while project_id in aliases:
        if project_id in seen:
            raise ValueError("Invalid project alias map: cycle")
        seen.add(project_id)
        project_id = aliases[project_id]
    return project_id


def _registered_project(cwd: str, env: Mapping[str, str]) -> ProjectIdentity | None:
    identity = _folder_identity(cwd) if cwd else None
    registered = env.get("AGENTIHOOKS_PROJECT_ID", "")
    if registered and cwd and not _git(Path(cwd), "rev-parse", "--git-common-dir"):
        if not re.fullmatch(r"local:[A-Za-z0-9][A-Za-z0-9._-]{0,127}", registered):
            raise ValueError("Invalid registered project ID")
        path = Path(cwd).expanduser().resolve()
        identity = identity or ProjectIdentity(path.name, path.name, cwd=str(path))
        return replace(identity, project_id=registered)
    return identity


def _swarm_identity(cwd: str, env: Mapping[str, str]) -> ProjectIdentity | None:
    swarm = env.get("AGENTIHOOKS_SWARM", "")
    if not swarm:
        return None
    from hooks.config import AGENTIHOOKS_HOME

    config = Path(AGENTIHOOKS_HOME) / "swarm" / swarm / "config.json"
    try:
        repo = json.loads(config.read_text()).get("repo", "")
    except (OSError, ValueError):
        repo = ""
    identity = _registered_project(repo, env) if repo else None
    if identity:
        working = _folder_identity(cwd) if cwd else None
        worktree = working.worktree if working and _same_checkout(working, repo) else identity.worktree
        return replace(identity, worktree=worktree, cwd=cwd or repo)
    return None


def resolve_project(
    cwd: str, env: Mapping[str, str] | None = None, *, aliases: Mapping[str, object] | None = None
) -> ProjectIdentity | None:
    env = os.environ if env is None else env
    alias_map = _project_aliases(aliases)
    identity = _swarm_identity(cwd, env) or _registered_project(cwd, env)
    if identity:
        return replace(identity, project_id=_aliased_project(identity.project_id, alias_map))
    return None
