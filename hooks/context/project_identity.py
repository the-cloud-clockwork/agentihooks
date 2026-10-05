import json
import os
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Mapping


@dataclass(frozen=True)
class ProjectIdentity:
    project: str
    repo: str
    worktree: str = ""
    cwd: str = ""
    remote: str = ""

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


def _folder_identity(cwd: str) -> ProjectIdentity | None:
    path = Path(cwd).expanduser().resolve()
    common = _git(path, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if common:
        root = Path(common).parent
        top = _git(path, "rev-parse", "--show-toplevel")
        remote = _git(path, "remote", "get-url", "origin").removesuffix(".git").rstrip("/")
        slug = remote.split("://")[-1].split(":")[-1]
        slug = "/".join(slug.split("/")[-2:]) if remote else root.name
        return ProjectIdentity(root.name, root.name, Path(top).name if top != str(root) else "", str(path), slug)
    scratch = Path.home() / "scratchpad"
    if path.is_relative_to(scratch) and len(path.relative_to(scratch).parts) >= 2:
        project = path.relative_to(scratch).parts[0]
        return ProjectIdentity(project, project, cwd=str(path))
    return None


def resolve_project(cwd: str, env: Mapping[str, str] | None = None) -> ProjectIdentity | None:
    env = os.environ if env is None else env
    swarm = env.get("AGENTIHOOKS_SWARM", "")
    if swarm:
        from hooks.config import AGENTIHOOKS_HOME

        config = Path(AGENTIHOOKS_HOME) / "swarm" / swarm / "config.json"
        try:
            repo = json.loads(config.read_text()).get("repo", "")
        except (OSError, ValueError):
            repo = ""
        if repo:
            identity = _folder_identity(repo)
            if identity:
                working = _folder_identity(cwd) if cwd else None
                worktree = working.worktree if working and working.repo == identity.repo else identity.worktree
                return ProjectIdentity(identity.project, identity.repo, worktree, cwd or repo, identity.remote)
    return _folder_identity(cwd) if cwd else None
