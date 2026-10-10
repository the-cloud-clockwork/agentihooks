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
from dataclasses import asdict, dataclass, replace
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from scripts.swarm import naming
from scripts.swarm_v2.filesystem import SEGMENT, Execution

SCHEMES = {"https", "ssh", "git", "file"}
SCP = re.compile(r"(?:[^@/]+@)?([^:/]+):(.+)")
BRANCH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]*")
COMMIT = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
LOCK = ".prepare.lock"
GIT_TIMEOUT = 600
TRACKING = "+refs/heads/*:refs/remotes/origin/*"
GIT_SCOPE = ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY")
BATCH_SSH = "ssh -o BatchMode=yes"


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
    found = None if "://" in url else SCP.fullmatch(url)
    try:
        parts = urlsplit(f"ssh://{found[1]}/{found[2]}" if found else url)
        host = (parts.hostname or "") + (f":{parts.port}" if parts.port else "")
    except ValueError:
        raise WorkspaceError("unsupported origin") from None
    path = parts.path.rstrip("/").removesuffix(".git")
    if parts.scheme not in SCHEMES or not path or (parts.scheme == "file") == bool(host):
        raise WorkspaceError("unsupported origin")
    return host + path


def mirror_path(execution: Execution, project: str, reuse: bool = True) -> Path:
    slug = re.sub(r"[^a-z0-9._-]+", "-", PurePosixPath(project).name.lower()).strip("-.") or "repo"
    digest = hashlib.sha256(project.encode()).hexdigest()[:16]
    suffix = "" if reuse else f"-{uuid.uuid4().hex[:8]}"
    return execution.path("checkout") / f"{slug}-{digest}{suffix}.git"


def _record_path(execution: Execution, task: str) -> Path:
    return execution.path("spool") / "workspaces" / f"{task}.json"


def recorded(execution: Execution, task: str) -> dict | None:
    path = _record_path(execution, task)
    return json.loads(path.read_text()) if path.is_file() else None


def _environ() -> dict[str, str]:
    environ = {key: value for key, value in os.environ.items() if key not in GIT_SCOPE}
    environ.setdefault("GIT_SSH_COMMAND", BATCH_SSH)
    return {**environ, "GIT_TERMINAL_PROMPT": "0"}


def _git(*args: str, repo: Path | None = None) -> subprocess.CompletedProcess:
    scope = ["--git-dir", str(repo)] if repo else []
    try:
        return subprocess.run(
            ["git", *scope, *args], capture_output=True, text=True, timeout=GIT_TIMEOUT, env=_environ()
        )
    except (OSError, subprocess.TimeoutExpired):
        raise WorkspaceError(f"git {args[0]} did not finish") from None


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
    if request.minimum and not COMMIT.fullmatch(request.minimum):
        raise WorkspaceError(f"invalid required commit: {request.minimum}")
    if naming.parse(request.agent) is None:
        raise WorkspaceError(f"invalid agent: {request.agent}")
    origin = request.origin.strip()
    parts = urlsplit(origin)
    userinfo, at, _ = origin.partition("@")
    scp_secret = bool(at) and "/" not in userinfo and ":" in userinfo
    if parts.password or (parts.username and parts.scheme == "https") or scp_secret:
        raise WorkspaceError("origin carries a credential; supply it through a credential helper")


def _replay(execution: Execution, request: Request, project: str) -> Workspace | None:
    held = recorded(execution, request.task)
    if held is None or held["generation"] < request.generation:
        return None
    attempt = f"task {request.task} generation {request.generation}"
    if held["generation"] > request.generation:
        raise WorkspaceError(f"{attempt} is older than its recorded generation {held['generation']}")
    if (held["project"], held["base"]) != (project, request.base):
        raise WorkspaceError(f"{attempt} is recorded for another project or base")
    return Workspace(**{**held, "path": Path(held["path"]), "mirror": Path(held["mirror"])})


def _verify(mirror: Path, project: str) -> None:
    found = _git("config", "remote.origin.url", repo=mirror)
    try:
        origin = identity(found.stdout) if found.returncode == 0 else "unknown"
    except WorkspaceError:
        origin = "unknown"
    if origin != project:
        raise WorkspaceError(f"cached mirror {mirror.name} has origin {origin}, not {project}; it is not reused")


def _clone(url: str, mirror: Path, project: str) -> None:
    staging = Path(tempfile.mkdtemp(dir=mirror.parent))
    try:
        if _git("clone", "--bare", url, str(staging)).returncode:
            raise WorkspaceError(f"clone of {project} failed")
    except WorkspaceError:
        shutil.rmtree(staging)
        raise
    staging.rename(mirror)


def _fetch(mirror: Path, project: str) -> None:
    if _git("fetch", "--atomic", "--prune", "origin", TRACKING, repo=mirror).returncode:
        raise WorkspaceError(f"fetch of {project} failed; the cached base is unverified, so work does not start")


def _base(mirror: Path, request: Request, project: str) -> str:
    found = _git("rev-parse", f"refs/remotes/origin/{request.base}^{{commit}}", repo=mirror)
    if found.returncode:
        raise WorkspaceError(f"base {request.base} is missing from {project}")
    commit = found.stdout.strip()
    if request.minimum and _git("merge-base", "--is-ancestor", request.minimum, commit, repo=mirror).returncode:
        raise WorkspaceError(f"base {request.base} at {commit} does not contain {request.minimum}; it is stale")
    return commit


def _branch(execution: Execution, mirror: Path, agent: str) -> str:
    heads = _git("branch", "--format=%(refname:short)", repo=mirror).stdout.split()
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


def _save(execution: Execution, workspace: Workspace) -> None:
    record = _record_path(execution, workspace.task)
    record.parent.mkdir(mode=0o700, exist_ok=True)
    staging = record.with_suffix(".partial")
    staging.write_text(json.dumps({**asdict(workspace), "path": str(workspace.path), "mirror": str(workspace.mirror)}))
    staging.replace(record)


def _materialize(workspace: Workspace) -> None:
    if workspace.path.is_dir():
        return
    if not workspace.mirror.is_dir():
        raise WorkspaceError(f"task {workspace.task} generation {workspace.generation} lost its mirror")
    _git("worktree", "prune", repo=workspace.mirror)
    held = _git("rev-parse", f"refs/heads/{workspace.branch}", repo=workspace.mirror)
    if held.returncode == 0:
        target = [str(workspace.path), workspace.branch]
    else:
        target = ["-b", workspace.branch, str(workspace.path), workspace.base_commit]
    if _git("worktree", "add", *target, repo=workspace.mirror).returncode:
        raise WorkspaceError(f"worktree {workspace.branch} could not be created")


def prepare(
    execution: Execution, request: Request, *, reuse: bool = True, clock: Callable[[], float] = time.monotonic
) -> Workspace:
    started = clock()
    project = identity(request.origin)
    _check(request)
    with _locked(execution):
        held = _replay(execution, request, project)
        if held is not None:
            _materialize(held)
            return held
        mirror = _mirror(execution, request, project, reuse)
        commit = _base(mirror, request, project)
        branch = _branch(execution, mirror, request.agent)
        path = execution.path("worktree") / branch
        planned = Workspace(path, branch, mirror, project, request.base, commit, request.task, request.generation, 0.0)
        _save(execution, planned)
        _materialize(planned)
        workspace = replace(planned, workspace_prepare_seconds=clock() - started)
        _save(execution, workspace)
        return workspace
