import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlsplit

from scripts.swarm import naming
from scripts.swarm_v2.filesystem import SEGMENT, Execution

SCHEMES = {"https", "http", "ssh", "git", "file"}
SCP = re.compile(r"(?:[^@/]+@)?([^:/]+):(.+)")
BRANCH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]*")
LOCK = ".prepare.lock"
GIT_TIMEOUT = 600
TRACKING = "+refs/heads/*:refs/remotes/origin/*"


class WorkspaceError(ValueError):
    pass


@dataclass(frozen=True)
class Request:
    origin: str
    task: str
    generation: int
    agent: str
    base: str = "dev"
    minimum: str = ""


@dataclass(frozen=True)
class Workspace:
    path: Path
    branch: str
    mirror: Path
    project: str
    base: str
    base_commit: str
    task: str
    generation: int
    workspace_prepare_seconds: float


def identity(url: str) -> str:
    url = url.strip()
    if "://" in url:
        parts = urlsplit(url)
        host = (parts.hostname or "") + (f":{parts.port}" if parts.port else "")
        scheme, path = parts.scheme, parts.path
    else:
        found = SCP.fullmatch(url)
        scheme, host, path = ("ssh", found[1].lower(), "/" + found[2]) if found else ("", "", "")
    path = path.rstrip("/").removesuffix(".git")
    if scheme not in SCHEMES or not path or (scheme != "file") != bool(host):
        raise WorkspaceError("unsupported origin")
    return host + path


def mirror_path(execution: Execution, project: str, reuse: bool = True) -> Path:
    slug = re.sub(r"[^a-z0-9._-]+", "-", project.rsplit("/", 1)[-1].lower()).strip("-.") or "repo"
    digest = hashlib.sha256(project.encode()).hexdigest()[:16]
    suffix = "" if reuse else f"-{uuid.uuid4().hex[:8]}"
    return execution.path("checkout") / f"{slug}-{digest}{suffix}.git"


def _record_path(execution: Execution, task: str) -> Path:
    return execution.path("spool") / "workspaces" / f"{task}.json"


def recorded(execution: Execution, task: str) -> dict | None:
    path = _record_path(execution, task)
    return json.loads(path.read_text()) if path.is_file() else None


def _git(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=GIT_TIMEOUT,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )


@contextmanager
def _locked(execution: Execution):
    with open(execution.path("checkout") / LOCK, "a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def _check(request: Request) -> None:
    if not SEGMENT.fullmatch(request.task):
        raise WorkspaceError(f"invalid task id: {request.task}")
    if request.generation < 1:
        raise WorkspaceError(f"invalid generation: {request.generation}")
    if not BRANCH.fullmatch(request.base):
        raise WorkspaceError(f"invalid base branch: {request.base}")


def _replay(execution: Execution, request: Request, project: str) -> Workspace | None:
    held = recorded(execution, request.task)
    if held is None or held["generation"] < request.generation:
        return None
    if held["generation"] > request.generation:
        older = f"task {request.task} generation {request.generation} is older"
        raise WorkspaceError(f"{older} than its recorded generation {held['generation']}")
    if held["project"] != project:
        raise WorkspaceError(f"task {request.task} generation {request.generation} is recorded for another project")
    return Workspace(**{**held, "path": Path(held["path"]), "mirror": Path(held["mirror"])})


def _verify(mirror: Path, project: str) -> None:
    found = _git("config", "remote.origin.url", cwd=mirror)
    try:
        origin = identity(found.stdout) if found.returncode == 0 else "unknown"
    except WorkspaceError:
        origin = "unknown"
    if origin != project:
        raise WorkspaceError(f"cached mirror {mirror.name} has origin {origin}, not {project}; it is not reused")


def _clone(url: str, mirror: Path, project: str) -> None:
    staging = Path(tempfile.mkdtemp(dir=mirror.parent))
    if _git("clone", "--bare", url, str(staging)).returncode:
        shutil.rmtree(staging)
        raise WorkspaceError(f"clone of {project} failed")
    staging.rename(mirror)


def _fetch(mirror: Path, project: str) -> None:
    if _git("fetch", "--atomic", "--prune", "origin", TRACKING, cwd=mirror).returncode:
        raise WorkspaceError(f"fetch of {project} failed; the cached base is unverified, so work does not start")


def _base(mirror: Path, request: Request, project: str) -> str:
    found = _git("rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{request.base}^{{commit}}", cwd=mirror)
    if found.returncode:
        raise WorkspaceError(f"base {request.base} is missing from {project}")
    commit = found.stdout.strip()
    if request.minimum and _git("merge-base", "--is-ancestor", request.minimum, commit, cwd=mirror).returncode:
        raise WorkspaceError(f"base {request.base} at {commit} does not contain {request.minimum}; it is stale")
    return commit


def _branch(execution: Execution, mirror: Path, agent: str) -> str:
    heads = _git("for-each-ref", "--format=%(refname:short)", "refs/heads", cwd=mirror).stdout.split()
    taken = {path.name for path in execution.path("worktree").iterdir()} | set(heads)
    return naming.worktree({"AGENTIHOOKS_AGENT_NAME": agent}, taken)


def _mirror(execution: Execution, request: Request, project: str, reuse: bool) -> Path:
    mirror = mirror_path(execution, project, reuse)
    if mirror.exists():
        _verify(mirror, project)
    else:
        _clone(request.origin, mirror, project)
    _fetch(mirror, project)
    return mirror


def prepare(
    execution: Execution, request: Request, *, reuse: bool = True, clock: Callable[[], float] = time.monotonic
) -> Workspace:
    started = clock()
    _check(request)
    project = identity(request.origin)
    with _locked(execution):
        replayed = _replay(execution, request, project)
        if replayed is not None:
            return replayed
        mirror = _mirror(execution, request, project, reuse)
        commit = _base(mirror, request, project)
        branch = _branch(execution, mirror, request.agent)
        path = execution.path("worktree") / branch
        if _git("worktree", "add", "-b", branch, str(path), commit, cwd=mirror).returncode:
            raise WorkspaceError(f"worktree {branch} could not be created")
        workspace = Workspace(
            path, branch, mirror, project, request.base, commit, request.task, request.generation, clock() - started
        )
        record = _record_path(execution, request.task)
        record.parent.mkdir(mode=0o700, exist_ok=True)
        record.write_text(json.dumps({**asdict(workspace), "path": str(path), "mirror": str(mirror)}))
        return workspace
