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


def _copy_tree(src: Path, dst: Path) -> None:
    """Python-native replacement for ``rsync -a src/ dst/``.

    Replicates rsync's trailing-slash semantics (copy contents, not the dir
    itself) using shutil.copytree with dirs_exist_ok. Avoids the rsync
    binary dependency, which isn't present on slim container images.
    """
    shutil.copytree(src, dst, dirs_exist_ok=True, symlinks=True)


def _mint_github_app_token() -> str | None:
    """Mint a fresh GitHub App installation token if GITHUB_APP_* env vars are set.

    Returns the token string on success, None if the env vars are missing or
    PyJWT/httpx aren't available. Caller falls back to whatever ``gh`` picks
    up from ``GITHUB_TOKEN`` / ``GH_TOKEN`` in that case.

    The token is short-lived (1h). Used per-call to authenticate ``gh pr
    create`` on pods where the static ``GITHUB_TOKEN`` env is a user PAT
    without repo scope for the mirror. git push already uses the same app
    via the credential helper.
    """
    pk = os.environ.get("GITHUB_APP_PRIVATE_KEY", "")
    app_id = os.environ.get("GITHUB_APP_ID", "")
    inst = os.environ.get("GITHUB_APP_INSTALLATION_ID", "")
    if not (pk and app_id and inst):
        return None
    try:
        import httpx
        import jwt
    except ImportError:
        _log("github-app token mint: PyJWT/httpx missing; gh will use GITHUB_TOKEN")
        return None
    try:
        now = int(time.time())
        j = jwt.encode(
            {"iat": now - 60, "exp": now + 540, "iss": app_id},
            pk,
            algorithm="RS256",
        )
        r = httpx.post(
            f"https://api.github.com/app/installations/{inst}/access_tokens",
            headers={"Authorization": f"Bearer {j}", "Accept": "application/vnd.github+json"},
            timeout=10.0,
        )
        if r.status_code >= 300:
            _log(f"github-app mint failed: {r.status_code} {r.text[:200]}")
            return None
        return r.json().get("token")
    except Exception as e:
        _log(f"github-app mint error: {e}")
        return None


def _gh_env() -> dict[str, str]:
    """Build env for gh subprocess, preferring a fresh App installation token.

    Falls back to inheriting ``os.environ`` when no App is configured — so
    laptop manual use keeps working with whatever the user has logged into
    ``gh auth login``.
    """
    env = os.environ.copy()
    token = _mint_github_app_token()
    if token:
        env["GH_TOKEN"] = token
        env["GITHUB_TOKEN"] = token  # some gh paths prefer one over the other
    return env


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


def _role() -> str:
    """Resolved fleet role (v4). Always one of: off/consumer/offline/
    contributor/authority. Derived in ``hooks/config.py`` from
    ``MEMORY_MIRROR_ROLE`` with legacy fallback to ``MEMORY_MIRROR_MODE`` /
    ``MEMORY_MIRROR_ENABLED``."""
    return (getattr(config, "MEMORY_MIRROR_ROLE", "off") or "off").lower()


# ---------------------------------------------------------------------------
# Identity resolver — v3 core
# ---------------------------------------------------------------------------

# Package / agent boundary markers. Lower priority number = wins over higher.
MARKER_PRIORITY: list[tuple[str, int]] = [
    ("agent.yml", 0),  # fleet-agent boundary (highest priority)
    ("pyproject.toml", 1),  # Python package
    ("Cargo.toml", 1),  # Rust crate
    ("package.json", 1),  # Node package
    ("go.mod", 1),  # Go module
    (".git", 2),  # repo root (fallback)
]


def _decode_encoded_path(encoded: str, root: Path = Path("/")) -> Path | None:
    """Reverse-walk the filesystem to recover the real path that produced a
    Claude project directory name.

    The encoding replaces every ``/`` with ``-``. Directory names may
    themselves contain ``-`` (``tcc-ecosystem``, ``tcc-toolbelt``), so we
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
        raise RuntimeError("MEMORY_MIRROR_REMOTE is not set. Add it to ~/.agentihooks/.env")

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
    """Wipe EVERYTHING in the mirror root except ``.git`` and recreate the
    two managed subtrees. This makes each snapshot authoritative — stale
    top-level dirs from legacy v2 layouts, old _unmapped buckets, or any
    cruft from previous ticks disappear instead of being re-published by
    ``git add -A``."""
    for child in mirror.iterdir():
        if child.name == ".git":
            continue
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()
    (mirror / "by-project").mkdir(parents=True, exist_ok=True)
    (mirror / "_unmapped").mkdir(parents=True, exist_ok=True)


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
            _copy_tree(source_memory, mem_dest)
            copied += 1
    _log(f"snapshot: mirrored memory for {copied} project(s) into {len(id_map)} identity bucket(s)")


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
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "agentihooks-memory-mirror",
        "GIT_AUTHOR_EMAIL": "memory-mirror@agentihooks.local",
        "GIT_COMMITTER_NAME": "agentihooks-memory-mirror",
        "GIT_COMMITTER_EMAIL": "memory-mirror@agentihooks.local",
    }
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
        ["git", "fetch", "--prune", "origin", "refs/heads/main:refs/remotes/origin/main"],
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
        slug = url[len("git@github.com:") :]
    elif url.startswith("https://github.com/"):
        slug = url[len("https://github.com/") :]
    else:
        return None
    if slug.endswith(".git"):
        slug = slug[:-4]
    return slug or None


def pull_only() -> None:
    """Event-driven consumer pull — used by the UserPromptSubmit hook.
    ensure_mirror_repo + fetch_remote + consume_main. No snapshot, no push.
    Fast no-op when role is off.
    """
    role = _role()
    if role == "off":
        return
    if not (config.MEMORY_MIRROR_REMOTE or "").strip():
        return
    ensure_mirror_repo()
    fetch_remote()
    consume_main()


def _copy_touched_to_mirror(touched_paths: list[str], mirror: Path) -> list[Path]:
    """Copy ONLY the files listed in touched_paths into the mirror tree.

    Each touched path is of the form ``/.../.claude/projects/<encoded>/memory/<file>``.
    Maps that to the mirror destination via the same identity resolver used
    by ``snapshot_in``:
        - mapped:   mirror/by-project/<key>/memory/<relative>
        - unmapped: mirror/_unmapped/<encoded>/memory/<relative>

    Returns the list of destination paths actually written (so the caller
    can ``git add`` exactly those paths — NOT ``git add -A``).
    """
    claude_projects = _claude_projects()
    dests: list[Path] = []
    for src_str in touched_paths:
        src = Path(src_str)
        if not src.is_file():
            _log(f"touched path vanished: {src_str}")
            continue
        try:
            rel = src.relative_to(claude_projects)
        except ValueError:
            _log(f"touched path outside projects root: {src_str}")
            continue
        # rel should look like "<encoded>/memory/<file...>"
        parts = rel.parts
        if len(parts) < 3 or parts[1] != "memory":
            _log(f"touched path not under memory/: {src_str}")
            continue
        encoded = parts[0]
        inside_memory = Path(*parts[2:])
        key, status = _identity_key(encoded)
        if status == "unmapped":
            dest_root = mirror / "_unmapped" / encoded / "memory"
        else:
            dest_root = mirror / "by-project" / key / "memory"
        dest = dest_root / inside_memory
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        dests.append(dest)
    return dests


def propose_pr(
    auto_merge: bool = False,
    session_id: str | None = None,
    agent_name: str | None = None,
    touched_paths: list[str] | None = None,
) -> int:
    """Open a PR promoting this node's current memory tree into main.

    v5 event-driven flow: snapshots local memory inline, writes the mirror
    tree to a commit parented on origin/main, pushes to a session-scoped
    branch, opens a PR. No dependency on a gitfoam host branch — contributor
    pods no longer run the 60s daemon.

    Branch: ``memory/<agent_name>/<session_id>`` (session-scoped so
    concurrent contributor sessions don't collide).
    Title:  ``memory: <agent_name> — <N> file(s) touched``.

    Returns 0 on success, 1 on noop (no diff vs main), 2 on error.
    """
    role = _role()
    if role != "contributor":
        _log(
            f"ERROR: propose requires role=contributor (current role={role}). Consumer/authority nodes do not open PRs."
        )
        return 2
    slug = _remote_slug()
    if not slug:
        _log("ERROR: MEMORY_MIRROR_REMOTE is not a github.com URL; cannot use `gh pr create`")
        return 2
    if shutil.which("gh") is None:
        _log("ERROR: `gh` CLI not found. Install GitHub CLI: https://cli.github.com/")
        return 2

    # Default naming for manual / daemon-era use.
    if not agent_name:
        agent_name = os.getenv("AGENTICORE_AGENT_NAME") or os.getenv("AGENT_NAME") or _hostname()
    if not session_id:
        session_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")

    mirror = ensure_mirror_repo()
    fetch_remote()

    if touched_paths:
        # Narrow path (v5 event-driven). Reset mirror working tree to match
        # origin/main so nothing stale leaks into the commit, copy ONLY the
        # session's touched files into their mirror destinations, then
        # ``git add`` exactly those paths.
        _run(["git", "reset", "--hard", "refs/remotes/origin/main"], cwd=mirror, check=False)
        _run(["git", "clean", "-fdx"], cwd=mirror, check=False)
        dests = _copy_touched_to_mirror(touched_paths, mirror)
        if not dests:
            _log("propose_pr: no mapped destinations for touched paths — nothing to push")
            return 1
        rel_paths = [str(d.relative_to(mirror)) for d in dests]
        add_proc = _run(["git", "add", *rel_paths], cwd=mirror, capture=True, check=False)
    else:
        # Wholesale path (legacy — authority daemon / manual CLI). Snapshot
        # everything, ``git add -A``. Produces multi-thousand-file PRs when
        # the mirror has accumulated unmerged state; use the narrow path
        # whenever the caller knows what files changed.
        snapshot_in()
        add_proc = _run(["git", "add", "-A"], cwd=mirror, capture=True, check=False)
    if add_proc.returncode != 0:
        _log(f"ERROR: git add -A failed: {(add_proc.stderr or '').strip()}")
        return 2
    tree_proc = _run(
        ["git", "write-tree"],
        cwd=mirror,
        capture=True,
        check=False,
    )
    tree_sha = (tree_proc.stdout or "").strip()
    if not tree_sha:
        _log(f"ERROR: write-tree failed: {(tree_proc.stderr or '').strip()}")
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
    main_tree_proc = _run(
        ["git", "rev-parse", f"{main_sha}^{{tree}}"],
        cwd=mirror,
        capture=True,
        check=False,
    )
    main_tree = (main_tree_proc.stdout or "").strip()
    if main_tree == tree_sha:
        _log("nothing to propose — mirror tree identical to main")
        return 1

    # Changed-file count for the title.
    diff_proc = _run(
        ["git", "diff", "--name-only", f"{main_sha}", tree_sha],
        cwd=mirror,
        capture=True,
        check=False,
    )
    changed_files = [line for line in (diff_proc.stdout or "").splitlines() if line.strip()]
    n_files = len(changed_files)

    proposal_branch = f"memory/{agent_name}/{session_id}"
    title = f"memory: {agent_name} — {n_files} file(s) touched"
    commit_msg = (
        f"{title}\n\n"
        f"Agent:    {agent_name}\n"
        f"Session:  {session_id}\n"
        f"Tree:     {tree_sha[:12]}\n"
        f"Based on: {main_sha[:12]} (origin/main)\n"
    )

    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "agentihooks-memory-mirror",
        "GIT_AUTHOR_EMAIL": "memory-mirror@agentihooks.local",
        "GIT_COMMITTER_NAME": "agentihooks-memory-mirror",
        "GIT_COMMITTER_EMAIL": "memory-mirror@agentihooks.local",
    }
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
        ["git", "push", "--force", "origin", f"{commit_sha}:refs/heads/{proposal_branch}"],
        cwd=mirror,
        capture=True,
        check=False,
    )
    if push.returncode != 0:
        _log(f"ERROR: failed to push {proposal_branch}: {(push.stderr or '').strip()}")
        return 2

    file_lines = "\n".join(f"- `{f}`" for f in changed_files[:20])
    if n_files > 20:
        file_lines += f"\n- … and {n_files - 20} more"
    body = (
        f"Agent **{agent_name}** wrote memory during session `{session_id}`.\n\n"
        f"**Files touched ({n_files}):**\n{file_lines}\n\n"
        f"Based on: `{main_sha[:12]}` (origin/main)\n"
        f"Tree: `{tree_sha[:12]}`\n\n"
        "_Generated by agentihooks memory-sync v5._\n"
    )
    create = _run(
        [
            "gh",
            "pr",
            "create",
            "--repo",
            slug,
            "--base",
            "main",
            "--head",
            proposal_branch,
            "--title",
            title,
            "--body",
            body,
        ],
        capture=True,
        check=False,
        env=_gh_env(),
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
            env=_gh_env(),
        )
        if merge.returncode != 0:
            _log(f"gh pr merge --auto failed (PR still open): {(merge.stderr or '').strip()}")
            return 2
        _log(f"auto-merge armed on {pr_url}")
    return 0


# ---------------------------------------------------------------------------
# Sweep — delete merged+idle branches
# ---------------------------------------------------------------------------


def sweep_branches(idle_days: int | None = None) -> int:
    role = _role()
    if role == "consumer":
        _log("ERROR: sweep-branches requires role=contributor or authority (consumer has no fleet branches to sweep).")
        return 0
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
        ["git", "for-each-ref", "--format=%(refname:short) %(committerdate:unix)", f"refs/remotes/origin/{prefix}/"],
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
        short = full_ref[len("origin/") :]
        try:
            committed_at = int(ts_str)
        except ValueError:
            continue
        if committed_at > cutoff:
            continue
        ancestor = _run(
            ["git", "merge-base", "--is-ancestor", full_ref, "refs/remotes/origin/main"],
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
        short = line[len("origin/") :]
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


def _authority_push_main() -> bool:
    """Commit the current mirror tree on top of origin/main and push with
    ``--force-with-lease``. Returns True if main advanced, False if the lease
    was invalidated (peer PR landed since fetch) or nothing to push.

    The lease refspec ``--force-with-lease=main:<OLD_SHA>`` tells the server:
    "only accept this push if origin/main is still at OLD_SHA." If a peer
    force-pushed (or the operator merged a PR) between our fetch and push,
    the lease check fails server-side and we abort cleanly — the next tick
    re-fetches and tries again.
    """
    mirror = _mirror_dir()
    rev = _run(
        ["git", "rev-parse", "refs/remotes/origin/main"],
        cwd=mirror,
        capture=True,
        check=False,
    )
    old_sha = (rev.stdout or "").strip()
    if not old_sha or rev.returncode != 0:
        _log("authority: origin/main not seeded; skipping push")
        return False

    _run(["git", "add", "-A"], cwd=mirror)
    tree_proc = _run(["git", "write-tree"], cwd=mirror, capture=True)
    tree_sha = (tree_proc.stdout or "").strip()
    if not tree_sha:
        _log("authority: git write-tree produced empty SHA; skipping push")
        return False

    # If the tree is identical to origin/main's tree, nothing to push.
    main_tree_proc = _run(
        ["git", "rev-parse", f"{old_sha}^{{tree}}"],
        cwd=mirror,
        capture=True,
        check=False,
    )
    if (main_tree_proc.stdout or "").strip() == tree_sha:
        return False

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "agentihooks-memory-mirror",
        "GIT_AUTHOR_EMAIL": "memory-mirror@agentihooks.local",
        "GIT_COMMITTER_NAME": "agentihooks-memory-mirror",
        "GIT_COMMITTER_EMAIL": "memory-mirror@agentihooks.local",
    }
    commit_proc = _run(
        ["git", "commit-tree", tree_sha, "-p", old_sha, "-m", f"authority: sync from {_hostname()} {ts}"],
        cwd=mirror,
        capture=True,
        env=env,
        check=False,
    )
    commit_sha = (commit_proc.stdout or "").strip()
    if not commit_sha:
        _log(f"authority: commit-tree failed: {(commit_proc.stderr or '').strip()}")
        return False

    _run(["git", "update-ref", "refs/heads/main", commit_sha], cwd=mirror, check=False)

    push_env = {**os.environ, "GIT_ALLOW_MAIN_PUSH": "1"}
    push = _run(
        ["git", "push", f"--force-with-lease=main:{old_sha}", "origin", "refs/heads/main:refs/heads/main"],
        cwd=mirror,
        capture=True,
        check=False,
        env=push_env,
    )
    if push.returncode != 0:
        stderr = (push.stderr or "").strip()
        _log(f"authority push lease invalidated (peer PR merged since fetch); will retry next tick: {stderr}")
        return False
    _log(f"authority pushed main @ {commit_sha[:12]}")
    return True


def tick() -> None:
    """One cycle: role-aware snapshot / fetch / consume / push (v4).

    Dispatch matrix:
      off          no-op
      consumer     fetch + consume only (no snapshot, no push)
      offline      snapshot only (no fetch, no consume)
      contributor  snapshot + fetch + consume (v3 default)
      authority    snapshot + fetch + consume + re-snapshot + force-with-lease
                   push to origin/main
    """
    role = _role()
    if role == "off":
        return
    if not (config.MEMORY_MIRROR_REMOTE or "").strip():
        _log("SKIP: MEMORY_MIRROR_REMOTE not set")
        return

    ensure_mirror_repo()

    if role == "consumer":
        fetch_remote()
        consume_main()
        return

    snapshot_in()

    if role == "offline":
        return

    fetch_remote()
    consume_main()

    if role == "authority":
        # Re-snapshot so any .conflict siblings consume_main just wrote are
        # included in the authoritative push.
        snapshot_in()
        _authority_push_main()


def main() -> int:
    try:
        tick()
    except Exception as exc:
        _log(f"ERROR: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
