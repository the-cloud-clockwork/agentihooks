#!/usr/bin/env python3
"""agentihooks memory mirror — cross-machine auto-memory sync.

Scope: ONLY ``~/.claude/projects/*/memory/`` subtrees. Transcripts,
session JSONLs, ctx_refresh snapshots, todos, etc. are explicitly excluded
by the rsync filter.

Architecture:
  push: rsync ~/.claude/projects/*/memory/ → MEMORY_MIRROR_DIR
        (gitfoam watches MEMORY_MIRROR_DIR and force-pushes to
        origin/<branch-prefix>/<hostname>/main every ~500ms)
  pull: git fetch origin 'refs/heads/<prefix>/*'
        for each remote branch ≠ self-hostname:
            git archive → temporary staging
            merge files into ~/.claude/projects/ — on byte-level conflict,
            write <name>.conflict-<host>-<epoch><ext> next to the target.

Usage (standalone, e.g. from cron as a fallback):
    python -m scripts.memory_mirror_sync

The module is also driven every poll by ``sync_daemon._run_daemon``.
"""

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from hooks import config  # noqa: E402

# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


def _log(msg: str) -> None:
    print(f"[memory-mirror] {msg}", flush=True)


def _run(
    cmd: list[str],
    cwd: Path | str | None = None,
    check: bool = True,
    capture: bool = False,
) -> subprocess.CompletedProcess:
    kwargs: dict = {}
    if cwd is not None:
        kwargs["cwd"] = str(cwd)
    if capture:
        kwargs["capture_output"] = True
        kwargs["text"] = True
    return subprocess.run(cmd, check=check, **kwargs)


def _hostname() -> str:
    return socket.gethostname()


def _branch_prefix() -> str:
    return (config.MEMORY_MIRROR_BRANCH_PREFIX or "gitfoam").strip("/")


def _mirror_dir() -> Path:
    return Path(os.path.expanduser(config.MEMORY_MIRROR_DIR))


def _claude_projects() -> Path:
    return Path(os.path.expanduser(config.MEMORY_MIRROR_CLAUDE_PROJECTS))


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------


def ensure_mirror_repo() -> Path:
    """Idempotently create MEMORY_MIRROR_DIR as a git repo with the configured
    remote. Returns the mirror path."""
    mirror = _mirror_dir()
    mirror.mkdir(parents=True, exist_ok=True)

    if not (mirror / ".git").is_dir():
        _run(["git", "init", "--initial-branch=main"], cwd=mirror)

    for key, default in (
        ("user.email", "memory-mirror@agentihooks.local"),
        ("user.name", "agentihooks-memory-mirror"),
    ):
        have = _run(["git", "config", "--get", key], cwd=mirror, capture=True, check=False)
        if have.returncode != 0 or not (have.stdout or "").strip():
            _run(["git", "config", key, default], cwd=mirror)

    remote = (config.MEMORY_MIRROR_REMOTE or "").strip()
    if not remote:
        raise RuntimeError(
            "MEMORY_MIRROR_REMOTE is not set. Add it to ~/.agentihooks/.env"
        )

    have_remote = _run(
        ["git", "remote", "get-url", "origin"],
        cwd=mirror,
        capture=True,
        check=False,
    )
    if have_remote.returncode != 0:
        _run(["git", "remote", "add", "origin", remote], cwd=mirror)
    else:
        current = (have_remote.stdout or "").strip()
        if current != remote:
            _run(["git", "remote", "set-url", "origin", remote], cwd=mirror)

    return mirror


# ---------------------------------------------------------------------------
# Push-side snapshot — memory-only rsync filter
# ---------------------------------------------------------------------------

# Applied relative to ~/.claude/projects/. Every path outside a */memory/
# subtree is dropped by the final --exclude='*'. Transcripts, sessions,
# ctx_refresh JSONs, and todos therefore never enter the mirror.
# The leading protect-filter keeps the mirror's own .git/ dir alive across
# --delete passes (rsync would otherwise wipe it because it's not in src).
RSYNC_MEMORY_FILTER: list[str] = [
    "--filter=P /.git",
    "--filter=P /.gitignore",
    "--prune-empty-dirs",
    "--include=*/",
    "--include=*/memory/",
    "--include=*/memory/**",
    "--exclude=*",
]


def snapshot_in() -> None:
    """rsync ~/.claude/projects/*/memory/ → MEMORY_MIRROR_DIR/"""
    src = _claude_projects()
    if not src.is_dir():
        _log(f"SKIP snapshot: {src} does not exist")
        return

    mirror = _mirror_dir()
    mirror.mkdir(parents=True, exist_ok=True)

    cmd = [
        "rsync",
        "-a",
        "--delete",
        *RSYNC_MEMORY_FILTER,
        f"{src}/",
        f"{mirror}/",
    ]
    _run(cmd)


# ---------------------------------------------------------------------------
# Pull-side consume
# ---------------------------------------------------------------------------


def fetch_remote() -> None:
    """git fetch origin refs/heads/<prefix>/*:refs/remotes/origin/<prefix>/*"""
    mirror = _mirror_dir()
    if not (mirror / ".git").is_dir():
        _log("SKIP fetch: mirror is not a git repo yet")
        return
    prefix = _branch_prefix()
    refspec = f"refs/heads/{prefix}/*:refs/remotes/origin/{prefix}/*"
    _run(
        ["git", "fetch", "--prune", "origin", refspec],
        cwd=mirror,
        check=False,
    )


def _list_remote_branches() -> list[str]:
    """Return short branch names like '<prefix>/<host>/main' for every
    origin/<prefix>/** ref."""
    mirror = _mirror_dir()
    prefix = _branch_prefix()
    proc = _run(
        [
            "git",
            "for-each-ref",
            "--format=%(refname:short)",
            f"refs/remotes/origin/{prefix}/",
        ],
        cwd=mirror,
        capture=True,
        check=False,
    )
    out: list[str] = []
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("origin/"):
            out.append(line[len("origin/") :])
    return out


def _is_self(branch: str) -> bool:
    """True if *branch* belongs to this hostname (loop-avoidance)."""
    return branch.startswith(f"{_branch_prefix()}/{_hostname()}/")


def _conflict_filename(target: Path) -> Path:
    """Produce <name>.conflict-<host>-<epoch><ext> next to *target*."""
    ts = int(time.time())
    host = _hostname()
    stem = target.stem or "memory"
    suffix = target.suffix
    return target.with_name(f"{stem}.conflict-{host}-{ts}{suffix}")


def _files_equal(a: Path, b: Path) -> bool:
    try:
        if a.stat().st_size != b.stat().st_size:
            return False
        return a.read_bytes() == b.read_bytes()
    except OSError:
        return False


def _merge_tree(staging: Path, target: Path) -> None:
    """Merge staging → target. Byte-level conflict writes a sibling
    .conflict-<host>-<epoch><ext> file; the original is never overwritten."""
    if not staging.is_dir():
        return
    for src_file in staging.rglob("*"):
        if not src_file.is_file():
            continue
        rel = src_file.relative_to(staging)
        dst_file = target / rel
        if dst_file.exists():
            if _files_equal(src_file, dst_file):
                continue
            conflict = _conflict_filename(dst_file)
            conflict.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_file, conflict)
        else:
            dst_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_file, dst_file)


def consume_remote_branches() -> None:
    """For every remote branch that isn't ours, archive its tree and
    merge its contents into ~/.claude/projects/."""
    mirror = _mirror_dir()
    if not (mirror / ".git").is_dir():
        return
    target = _claude_projects()
    target.mkdir(parents=True, exist_ok=True)

    for branch in _list_remote_branches():
        if _is_self(branch):
            continue
        with tempfile.TemporaryDirectory(prefix="agentihooks-mm-") as tmp:
            staging = Path(tmp)
            archive = subprocess.Popen(
                ["git", "archive", "--format=tar", f"origin/{branch}"],
                cwd=str(mirror),
                stdout=subprocess.PIPE,
            )
            extract = subprocess.Popen(
                ["tar", "-x", "-C", str(staging)],
                stdin=archive.stdout,
            )
            if archive.stdout:
                archive.stdout.close()
            extract.communicate()
            archive.wait()
            if extract.returncode != 0 or archive.returncode != 0:
                _log(f"archive/extract failed for {branch} (skipping)")
                continue
            _merge_tree(staging, target)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def tick() -> None:
    """One cycle: ensure repo → snapshot local → fetch remote → merge."""
    if not config.MEMORY_MIRROR_ENABLED:
        return
    if not (config.MEMORY_MIRROR_REMOTE or "").strip():
        _log("SKIP: MEMORY_MIRROR_REMOTE not set")
        return
    ensure_mirror_repo()
    snapshot_in()
    fetch_remote()
    consume_remote_branches()


def main() -> int:
    try:
        tick()
    except Exception as exc:
        _log(f"ERROR: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
