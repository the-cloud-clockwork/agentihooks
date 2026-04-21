#!/usr/bin/env python3
"""agentihooks memory mirror — cross-machine auto-memory sync (v3).

Scope: ONLY ``~/.claude/projects/*/memory/`` subtrees. Transcripts, session
JSONLs, and anything outside ``memory/`` are excluded by sourcing only the
memory/ subdir per project.

v3 topology — identity-keyed layout:
  write: for each ~/.claude/projects/<encoded>/, resolve the identity
         (repo / agent name) via reverse-walk of the filesystem + package
         boundary detection (agent.yml > pyproject/Cargo/package.json/go.mod
         > .git). rsync <encoded>/memory/ into MIRROR_DIR/by-project/<key>/memory/.
         Unresolvable keys land in MIRROR_DIR/_unmapped/<encoded>/memory/.
  push:  gitfoam force-pushes MIRROR_DIR to origin/gitfoam/<hostname>/main
         every ~500ms.
  pull:  git fetch origin main → git archive origin/main → tar -x → for
         each staging/by-project/<key>/memory/, apply to EVERY local
         ~/.claude/projects/<encoded>/memory/ that resolves to <key>.
         Divergent local files get <name>.conflict-<host>-<epoch><ext>.
  propose: build a proposal branch rooted at origin/main with gitfoam/<host>/main's
         tree on top, open PR via `gh pr create`.

Usage (standalone):
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
from datetime import datetime, timezone
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
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    kwargs: dict = {}
    if cwd is not None:
        kwargs["cwd"] = str(cwd)
    if capture:
        kwargs["capture_output"] = True
        kwargs["text"] = True
    if env is not None:
        kwargs["env"] = env
    return subprocess.run(cmd, check=check, **kwargs)


def _hostname() -> str:
    return socket.gethostname()


def _branch_prefix() -> str:
    return (config.MEMORY_MIRROR_BRANCH_PREFIX or "gitfoam").strip("/")


def _self_branch() -> str:
    return f"{_branch_prefix()}/{_hostname()}/main"


def _mirror_dir() -> Path:
    return Path(os.path.expanduser(config.MEMORY_MIRROR_DIR))


def _claude_projects() -> Path:
    return Path(os.path.expanduser(config.MEMORY_MIRROR_CLAUDE_PROJECTS))


def _mode() -> str:
    return (config.MEMORY_MIRROR_MODE or "off").lower()


# ---------------------------------------------------------------------------
# Identity resolver — v3 core
# ---------------------------------------------------------------------------

# Package / agent boundary markers. Lower priority number = wins over higher.
MARKER_PRIORITY: list[tuple[str, int]] = [
    ("agent.yml", 0),          # fleet-agent boundary (highest priority)
    ("pyproject.toml", 1),     # Python package
    ("Cargo.toml", 1),         # Rust crate
    ("package.json", 1),       # Node package
    ("go.mod", 1),             # Go module
    (".git", 2),               # repo root (fallback)
]


def _decode_encoded_path(encoded: str, root: Path = Path("/")) -> Path | None:
    """Reverse-walk the filesystem to recover the real path that produced a
    Claude project directory name.

    The encoding replaces every ``/`` with ``-``. Directory names may
    themselves contain ``-`` (``tccw-ecosystem``, ``tccw-toolbelt``), so we
    try to consume 1 segment, then 2, then 3, etc. at each level, matching
    against what actually exists on disk. Backtracks on dead ends.
    Returns ``None`` if the encoded path cannot be resolved.
    """
    parts = encoded.lstrip("-").split("-")
    if not parts or parts == [""]:
        return None

    def walk(i: int, cur: Path) -> Path | None:
        if i >= len(parts):
            return cur
        for end in range(i + 1, len(parts) + 1):
            name = "-".join(parts[i:end])
            nxt = cur / name
            if nxt.is_dir():
                found = walk(end, nxt)
                if found is not None:
                    return found
        return None

    return walk(0, root)


def _package_boundary(real_path: Path) -> tuple[Path, str] | None:
    """Walk up from *real_path* looking for package / agent boundary markers.
    Returns ``(boundary_dir, marker_name)`` or ``None``.

    Stops at the first ``.git`` ancestor (does not cross repo boundaries).
    Among all candidates collected along the walk, picks the one with the
    lowest priority number; ties broken by smallest distance from
    *real_path*.
    """
    candidates: list[tuple[int, int, Path, str]] = []
    cur = real_path
    distance = 0
    while True:
        best_here: tuple[int, str] | None = None
        for marker, prio in MARKER_PRIORITY:
            path = cur / marker
            exists = path.exists() if marker == ".git" else path.is_file()
            if exists:
                if best_here is None or prio < best_here[0]:
                    best_here = (prio, marker)
        if best_here is not None:
            candidates.append((best_here[0], distance, cur, best_here[1]))
        if (cur / ".git").exists():
            break  # do not cross repo root
        parent = cur.parent
        if parent == cur:
            break
        cur = parent
        distance += 1

    if not candidates:
        return None
    candidates.sort(key=lambda x: (x[0], x[1]))
    _, _, path, marker = candidates[0]
    return path, marker


def _identity_key(encoded: str) -> tuple[str, str]:
    """Resolve ``encoded`` to an identity key.
    Returns ``(key, status)`` where status is ``"ok"`` or ``"unmapped"``.

    ``ok``: ``key`` is ``basename()`` of the resolved package/agent boundary.
    ``unmapped``: ``key`` is the original encoded string (fallback bucket).
    """
    real = _decode_encoded_path(encoded)
    if real is None:
        return (encoded, "unmapped")
    boundary = _package_boundary(real)
    if boundary is None:
        return (encoded, "unmapped")
    return (boundary[0].name, "ok")


def _identity_map() -> dict[str, list[str]]:
    """Walk ``~/.claude/projects/*/`` and group encoded dirs by identity key.
    Unresolvable dirs land under pseudo-keys ``_unmapped/<encoded>``.
    """
    projects = _claude_projects()
    out: dict[str, list[str]] = {}
    if not projects.is_dir():
        return out
    for child in projects.iterdir():
        if not child.is_dir():
            continue
        key, status = _identity_key(child.name)
        if status == "unmapped":
            key = f"_unmapped/{child.name}"
        out.setdefault(key, []).append(child.name)
    return out


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
# Push-side snapshot — v3 by-project layout
# ---------------------------------------------------------------------------


def _prepare_layout_dirs(mirror: Path) -> None:
    """Wipe and recreate ``by-project/`` and ``_unmapped/`` so each snapshot
    has authoritative delete semantics per-key (removed local projects stop
    appearing in the mirror). .git/ is untouched."""
    for sub in ("by-project", "_unmapped"):
        target = mirror / sub
        if target.is_dir():
            shutil.rmtree(target)
        target.mkdir(parents=True, exist_ok=True)


def snapshot_in() -> None:
    """Per-project rsync: ~/.claude/projects/<encoded>/memory/ →
    MIRROR/by-project/<identity>/memory/ (or _unmapped/<encoded>/memory/)."""
    src = _claude_projects()
    if not src.is_dir():
        _log(f"SKIP snapshot: {src} does not exist")
        return

    mirror = _mirror_dir()
    mirror.mkdir(parents=True, exist_ok=True)
    _prepare_layout_dirs(mirror)

    id_map = _identity_map()
    copied = 0
    for key, encoded_dirs in id_map.items():
        if key.startswith("_unmapped/"):
            dest = mirror / key  # mirror/_unmapped/<encoded>
        else:
            dest = mirror / "by-project" / key
        mem_dest = dest / "memory"
        for encoded in encoded_dirs:
            source_memory = src / encoded / "memory"
            if not source_memory.is_dir():
                continue
            mem_dest.mkdir(parents=True, exist_ok=True)
            _run([
                "rsync",
                "-a",
                f"{source_memory}/",
                f"{mem_dest}/",
            ])
            copied += 1
    _log(f"snapshot: mirrored memory for {copied} project(s) into "
         f"{len(id_map)} identity bucket(s)")


# ---------------------------------------------------------------------------
# Seed — first-install push of main
# ---------------------------------------------------------------------------


def _remote_has_main() -> bool:
    mirror = _mirror_dir()
    probe = _run(
        ["git", "ls-remote", "--heads", "origin", "main"],
        cwd=mirror,
        capture=True,
        check=False,
    )
    if probe.returncode != 0:
        return False
    # ls-remote prints `<sha> refs/heads/main`. Must match the FULL ref.
    for line in (probe.stdout or "").splitlines():
        parts = line.strip().split()
        if len(parts) == 2 and parts[1] == "refs/heads/main":
            return True
    return False


def _commit_current_tree(mirror: Path, *, parents: list[str], message: str) -> str:
    """Stage everything in *mirror*, write a tree, commit it with *parents*
    and *message*. Returns the commit SHA."""
    _run(["git", "add", "-A"], cwd=mirror)
    tree_proc = _run(["git", "write-tree"], cwd=mirror, capture=True)
    tree_sha = (tree_proc.stdout or "").strip()
    if not tree_sha:
        raise RuntimeError("git write-tree produced empty SHA")
    env = {**os.environ,
           "GIT_AUTHOR_NAME": "agentihooks-memory-mirror",
           "GIT_AUTHOR_EMAIL": "memory-mirror@agentihooks.local",
           "GIT_COMMITTER_NAME": "agentihooks-memory-mirror",
           "GIT_COMMITTER_EMAIL": "memory-mirror@agentihooks.local"}
    cmd = ["git", "commit-tree", tree_sha]
    for p in parents:
        cmd += ["-p", p]
    cmd += ["-m", message]
    commit_proc = _run(cmd, cwd=mirror, capture=True, env=env)
    commit_sha = (commit_proc.stdout or "").strip()
    if not commit_sha:
        raise RuntimeError("git commit-tree produced empty SHA")
    return commit_sha


def _push_main(commit_sha: str, *, force: bool = False) -> None:
    mirror = _mirror_dir()
    _run(["git", "update-ref", "refs/heads/main", commit_sha], cwd=mirror)
    push_env = {**os.environ, "GIT_ALLOW_MAIN_PUSH": "1"}
    refspec = f"{'+' if force else ''}refs/heads/main:refs/heads/main"
    _run(
        ["git", "push", "origin", refspec],
        cwd=mirror,
        env=push_env,
    )


def seed_main() -> bool:
    """If origin/main doesn't exist, create it from the current local snapshot.
    Returns True if a seed commit was pushed.

    Uses commit-tree + update-ref + bypass of the operator's main-prod-lockdown
    hook (memory-mirror is a data repo, not a code repo)."""
    ensure_mirror_repo()
    if _remote_has_main():
        return False
    snapshot_in()
    mirror = _mirror_dir()
    commit_sha = _commit_current_tree(
        mirror,
        parents=[],
        message=f"seed main from {_hostname()}",
    )
    _push_main(commit_sha)
    _log(f"seeded origin/main @ {commit_sha[:12]}")
    return True


# ---------------------------------------------------------------------------
# Pull-side consume — v3 by-project layout
# ---------------------------------------------------------------------------


def fetch_remote() -> None:
    """git fetch origin main (+ prune stale refs)."""
    mirror = _mirror_dir()
    if not (mirror / ".git").is_dir():
        _log("SKIP fetch: mirror is not a git repo yet")
        return
    _run(
        ["git", "fetch", "--prune", "origin",
         "refs/heads/main:refs/remotes/origin/main"],
        cwd=mirror,
        check=False,
    )


def _origin_main_exists() -> bool:
    mirror = _mirror_dir()
    proc = _run(
        ["git", "for-each-ref", "--format=%(refname)", "refs/remotes/origin/main"],
        cwd=mirror,
        capture=True,
        check=False,
    )
    return bool((proc.stdout or "").strip())


def _files_equal(a: Path, b: Path) -> bool:
    try:
        if a.stat().st_size != b.stat().st_size:
            return False
        return a.read_bytes() == b.read_bytes()
    except OSError:
        return False


def _conflict_filename(target: Path) -> Path:
    ts = int(time.time())
    host = _hostname()
    stem = target.stem or "memory"
    suffix = target.suffix
    return target.with_name(f"{stem}.conflict-{host}-{ts}{suffix}")


def _merge_tree(staging: Path, target: Path) -> None:
    """Merge staging → target. Byte-level conflict writes a sibling
    .conflict-<host>-<epoch><ext>; the original is never overwritten."""
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


def consume_main() -> None:
    """Archive origin/main into a temp tree and merge each by-project/<key>/
    bucket into EVERY local encoded dir that resolves to <key>.

    Noop if origin/main absent or by-project/ missing."""
    mirror = _mirror_dir()
    if not (mirror / ".git").is_dir():
        return
    if not _origin_main_exists():
        _log("SKIP consume: origin/main not present yet (run `memory-sync install` to seed)")
        return

    target_root = _claude_projects()
    target_root.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="agentihooks-mm-") as tmp:
        staging = Path(tmp)
        archive = subprocess.Popen(
            ["git", "archive", "--format=tar", "origin/main"],
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
            _log("archive/extract failed for origin/main")
            return

        by_proj = staging / "by-project"
        if not by_proj.is_dir():
            _log("origin/main has no by-project/ tree yet — nothing to consume")
            return

        id_map = _identity_map()
        merged = 0
        for key_dir in by_proj.iterdir():
            if not key_dir.is_dir():
                continue
            key = key_dir.name
            mem = key_dir / "memory"
            if not mem.is_dir():
                continue
            local_encodeds = id_map.get(key, [])
            if not local_encodeds:
                continue  # no local project resolves to this identity
            for encoded in local_encodeds:
                local_memory = target_root / encoded / "memory"
                local_memory.mkdir(parents=True, exist_ok=True)
                _merge_tree(mem, local_memory)
                merged += 1
        _log(f"consume: merged main into {merged} local project memory dir(s)")


# ---------------------------------------------------------------------------
# Propose PR — promote gitfoam/<host>/main to main
# ---------------------------------------------------------------------------


def _remote_slug() -> str | None:
    url = (config.MEMORY_MIRROR_REMOTE or "").strip()
    if not url:
        return None
    if url.startswith("git@github.com:"):
        slug = url[len("git@github.com:"):]
    elif url.startswith("https://github.com/"):
        slug = url[len("https://github.com/"):]
    else:
        return None
    if slug.endswith(".git"):
        slug = slug[:-4]
    return slug or None


def propose_pr(auto_merge: bool = False) -> int:
    """Open a PR promoting the machine's tree into main via a proposal branch
    rooted at origin/main. Returns 0 on success, 1 on noop, 2 on error."""
    slug = _remote_slug()
    if not slug:
        _log("ERROR: MEMORY_MIRROR_REMOTE is not a github.com URL; cannot use `gh pr create`")
        return 2
    if shutil.which("gh") is None:
        _log("ERROR: `gh` CLI not found. Install GitHub CLI: https://cli.github.com/")
        return 2

    mirror = _mirror_dir()
    host_branch = _self_branch()
    _run(["git", "fetch", "--prune", "origin"], cwd=mirror, check=False)

    diff = _run(
        ["git", "diff", "--quiet", "origin/main", f"refs/remotes/origin/{host_branch}"],
        cwd=mirror,
        check=False,
    )
    if diff.returncode == 0:
        _log(f"{host_branch} has same tree as main — nothing to propose")
        return 1

    rev = _run(
        ["git", "rev-parse", f"refs/remotes/origin/{host_branch}^{{tree}}"],
        cwd=mirror,
        capture=True,
        check=False,
    )
    tree_sha = (rev.stdout or "").strip()
    if not tree_sha:
        _log(f"ERROR: could not resolve tree for {host_branch}")
        return 2

    main_rev = _run(
        ["git", "rev-parse", "refs/remotes/origin/main"],
        cwd=mirror,
        capture=True,
        check=False,
    )
    main_sha = (main_rev.stdout or "").strip()
    if not main_sha:
        _log("ERROR: origin/main not found; run `memory-sync install` first to seed it")
        return 2

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    proposal_branch = f"proposal/{_hostname()}/{today}-{tree_sha[:8]}"
    title = f"memory: promote {_hostname()} {today}"
    commit_msg = f"{title}\n\nTree: {tree_sha[:12]}\nSource: {host_branch}\n"

    env = {**os.environ,
           "GIT_AUTHOR_NAME": "agentihooks-memory-mirror",
           "GIT_AUTHOR_EMAIL": "memory-mirror@agentihooks.local",
           "GIT_COMMITTER_NAME": "agentihooks-memory-mirror",
           "GIT_COMMITTER_EMAIL": "memory-mirror@agentihooks.local"}
    commit_proc = _run(
        ["git", "commit-tree", tree_sha, "-p", main_sha, "-m", commit_msg],
        cwd=mirror,
        capture=True,
        env=env,
        check=False,
    )
    commit_sha = (commit_proc.stdout or "").strip()
    if not commit_sha:
        _log(f"ERROR: commit-tree failed: {(commit_proc.stderr or '').strip()}")
        return 2

    push = _run(
        ["git", "push", "origin",
         f"{commit_sha}:refs/heads/{proposal_branch}"],
        cwd=mirror,
        capture=True,
        check=False,
    )
    if push.returncode != 0:
        _log(f"ERROR: failed to push {proposal_branch}: {(push.stderr or '').strip()}")
        return 2

    body = "\n".join([
        f"Promote memory tree from `{host_branch}` → `main`.",
        "",
        f"Host:  `{_hostname()}`",
        f"Tree:  `{tree_sha[:12]}`",
        f"Based: `{main_sha[:12]}` (origin/main)",
    ])
    create = _run(
        ["gh", "pr", "create",
         "--repo", slug,
         "--base", "main",
         "--head", proposal_branch,
         "--title", title,
         "--body", body],
        capture=True,
        check=False,
    )
    if create.returncode != 0:
        _log(f"gh pr create failed: {(create.stderr or '').strip()}")
        return 2
    pr_url = (create.stdout or "").strip()
    _log(f"PR opened: {pr_url}")

    if auto_merge:
        merge = _run(
            ["gh", "pr", "merge", "--auto", "--squash", pr_url],
            capture=True,
            check=False,
        )
        if merge.returncode != 0:
            _log(f"gh pr merge --auto failed (PR still open): "
                 f"{(merge.stderr or '').strip()}")
            return 2
        _log(f"auto-merge armed on {pr_url}")
    return 0


# ---------------------------------------------------------------------------
# Sweep — delete merged+idle branches
# ---------------------------------------------------------------------------


def sweep_branches(idle_days: int | None = None) -> int:
    if idle_days is None:
        idle_days = config.MEMORY_MIRROR_SWEEP_IDLE_DAYS
    cutoff = time.time() - (idle_days * 86400)

    mirror = _mirror_dir()
    if not (mirror / ".git").is_dir():
        _log("SKIP sweep: mirror is not a git repo yet")
        return 0

    _run(["git", "fetch", "--prune", "origin"], cwd=mirror, check=False)
    if not _origin_main_exists():
        _log("SKIP sweep: origin/main not present yet")
        return 0

    prefix = _branch_prefix()
    listing = _run(
        ["git", "for-each-ref",
         "--format=%(refname:short) %(committerdate:unix)",
         f"refs/remotes/origin/{prefix}/"],
        cwd=mirror,
        capture=True,
        check=False,
    )
    deleted = 0
    for line in (listing.stdout or "").splitlines():
        parts = line.strip().split()
        if len(parts) != 2:
            continue
        full_ref, ts_str = parts
        if not full_ref.startswith("origin/"):
            continue
        short = full_ref[len("origin/"):]
        try:
            committed_at = int(ts_str)
        except ValueError:
            continue
        if committed_at > cutoff:
            continue
        ancestor = _run(
            ["git", "merge-base", "--is-ancestor",
             full_ref, "refs/remotes/origin/main"],
            cwd=mirror,
            check=False,
        )
        if ancestor.returncode != 0:
            continue
        push = _run(
            ["git", "push", "origin", "--delete", short],
            cwd=mirror,
            capture=True,
            check=False,
        )
        if push.returncode == 0:
            _log(f"swept {short} (merged, idle {int((time.time() - committed_at) / 86400)}d)")
            deleted += 1
        else:
            _log(f"failed to delete {short}: {(push.stderr or '').strip()}")
    return deleted


# ---------------------------------------------------------------------------
# Migrate layout — one-off rewrite to v3 layout
# ---------------------------------------------------------------------------


def list_non_main_remote_branches() -> list[str]:
    """Return all remote branches except main/HEAD."""
    mirror = _mirror_dir()
    _run(["git", "fetch", "--prune", "origin"], cwd=mirror, check=False)
    proc = _run(
        ["git", "for-each-ref", "--format=%(refname:short)", "refs/remotes/origin/"],
        cwd=mirror,
        capture=True,
        check=False,
    )
    out: list[str] = []
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("origin/"):
            continue
        short = line[len("origin/"):]
        if short in ("main", "HEAD"):
            continue
        out.append(short)
    return out


def delete_remote_branches(branches: list[str]) -> int:
    """Delete remote branches. Returns count of successful deletes."""
    if not branches:
        return 0
    mirror = _mirror_dir()
    deleted = 0
    for b in branches:
        push = _run(
            ["git", "push", "origin", "--delete", b],
            cwd=mirror,
            capture=True,
            check=False,
        )
        if push.returncode == 0:
            _log(f"deleted origin/{b}")
            deleted += 1
        else:
            _log(f"failed to delete {b}: {(push.stderr or '').strip()}")
    return deleted


def force_reseed_main() -> str:
    """Wipe the local mirror working tree contents (not .git), re-snapshot,
    and force-push a fresh main. Returns the new commit SHA."""
    mirror = _mirror_dir()
    # Wipe everything except .git
    for child in mirror.iterdir():
        if child.name == ".git":
            continue
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()
    # Clear the index so `git add -A` picks up a fresh state
    _run(["git", "read-tree", "--empty"], cwd=mirror, check=False)

    snapshot_in()
    commit_sha = _commit_current_tree(
        mirror,
        parents=[],
        message=f"layout: migrate to by-project/<key>/ v3 from {_hostname()}",
    )
    _push_main(commit_sha, force=True)
    return commit_sha


def plan_migrate_layout() -> dict:
    """Produce a dry-run plan showing what migrate_layout would do."""
    mirror = _mirror_dir()
    ensure_mirror_repo()
    branches = list_non_main_remote_branches()
    id_map = _identity_map()
    ok_keys = {k: v for k, v in id_map.items() if not k.startswith("_unmapped/")}
    unmapped_keys = {k: v for k, v in id_map.items() if k.startswith("_unmapped/")}
    return {
        "mirror": str(mirror),
        "branches_to_delete": branches,
        "identities": sorted(ok_keys.keys()),
        "unmapped_encoded": [k.split("/", 1)[1] for k in sorted(unmapped_keys.keys())],
        "local_projects": sum(len(v) for v in id_map.values()),
    }


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def tick() -> None:
    """One cycle: mode-aware snapshot → fetch main → merge."""
    mode = _mode()
    if mode == "off":
        return
    if not (config.MEMORY_MIRROR_REMOTE or "").strip():
        _log("SKIP: MEMORY_MIRROR_REMOTE not set")
        return

    ensure_mirror_repo()
    snapshot_in()
    if mode == "write-local-only":
        return
    fetch_remote()
    consume_main()


def main() -> int:
    try:
        tick()
    except Exception as exc:
        _log(f"ERROR: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
