#!/usr/bin/env python3
"""agentihooks sync daemon — watches source files and auto-reinstalls
when changes are detected.

Usage (via CLI):
  agentihooks daemon start              # start background daemon
  agentihooks daemon stop               # stop daemon
  agentihooks daemon status             # show daemon state
  agentihooks daemon logs               # tail daemon log
  agentihooks daemon start --foreground # run in foreground (debug)
"""

import argparse
import fcntl
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

AGENTIHOOKS_ROOT = Path(__file__).resolve().parent.parent
AGENTIHOOKS_STATE_DIR = Path(os.environ.get("AGENTIHOOKS_HOME", str(Path.home() / ".agentihooks")))

# Ensure the agentihooks package root is importable when this script is
# launched directly via `python sync_daemon.py` from any cwd. Without this,
# `from hooks.context.broadcast import ...` fails with ModuleNotFoundError.
if str(AGENTIHOOKS_ROOT) not in sys.path:
    sys.path.insert(0, str(AGENTIHOOKS_ROOT))
STATE_JSON = AGENTIHOOKS_STATE_DIR / "state.json"
HASH_FILE = AGENTIHOOKS_STATE_DIR / "sync-hashes.json"
PID_FILE = AGENTIHOOKS_STATE_DIR / "sync-daemon.pid"
SINGLETON_LOCK_FILE = AGENTIHOOKS_STATE_DIR / ".sync-daemon.singleton.lock"
LOG_FILE = AGENTIHOOKS_STATE_DIR / "logs" / "sync-daemon.log"
LOCK_FILE = AGENTIHOOKS_STATE_DIR / "sync.lock"
PROFILES_DIR = AGENTIHOOKS_ROOT / "profiles"


# Claude Code user config — for MCP server tracking
def _resolve_claude_json() -> Path:
    home_dir = os.environ.get("CLAUDE_CODE_HOME_DIR")
    if home_dir:
        return Path(home_dir) / ".claude.json"
    return Path.home() / ".claude.json"


CLAUDE_JSON = _resolve_claude_json()

DEFAULT_POLL_SEC = 60

# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------


def _log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[sync {ts}] {msg}", flush=True)


def _sha256(path: Path) -> str | None:
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                h.update(chunk)
        return h.hexdigest()
    except (OSError, PermissionError):
        return None


def _write_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


class CorruptStateError(Exception):
    """Raised by _load_json_strict on JSONDecodeError. Caught at call sites
    that need to log loudly + skip the affected step (C3)."""

    def __init__(self, path: Path, original: Exception):
        super().__init__(f"corrupt JSON at {path}: {original}")
        self.path = path
        self.original = original


def _load_json(path: Path) -> dict:
    """Permissive loader — returns {} on any error. Suitable for
    best-effort reads where corruption should not halt a step."""
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _load_json_strict(path: Path) -> dict:
    """Strict loader — returns {} when the file does not exist (legitimate
    empty state) but raises CorruptStateError when the file exists and is
    unparseable. Use at sites that touch state.json / ~/.claude.json /
    HASH_FILE / known-mcp-servers.json so corruption is surfaced loudly
    instead of being silently treated as empty."""
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise CorruptStateError(path, e) from e
    except OSError as e:
        # I/O errors are transient; treat as empty rather than corrupt.
        _log(f"WARN: read error on {path}: {e}")
        return {}


def _log_corrupt(path: Path, err: Exception, action: str = "skipping this step until resolved") -> None:
    """Multi-line ERROR block used by every site that catches CorruptStateError."""
    _log(f"CORRUPT JSON: {path}")
    _log(f"  error: {err}")
    _log(f"  {action}")


# Critical top-level keys in state.json. If a state-mutating helper would
# write a state object that has lost ALL of these (because _load_json
# returned {} on a transient read failure), the write is REFUSED — that
# prevents the daemon from blanking the operator's bundle/linked-profiles
# registration during a hiccup. See _safe_state_update.
_STATE_CRITICAL_KEYS = frozenset({"bundle", "linked_profiles", "targets", "version"})


def _safe_state_update(mutator) -> bool:
    """Read state.json strictly, apply *mutator(state)*, write back atomically.

    Returns True when the write succeeded; False when the cycle was refused
    (read corruption, or the resulting state would have lost every critical
    key on disk). The refusal path is the protection against the long-standing
    bundle-strip defect where helpers used permissive _load_json + merge +
    write — losing bundle/linked_profiles/targets if state was momentarily
    unreadable.
    """
    try:
        state = _load_json_strict(STATE_JSON)
    except CorruptStateError as e:
        _log_corrupt(e.path, e.original, action="state.json corrupt — write SKIPPED to preserve prior content")
        return False
    pre_critical = set(state.keys()) & _STATE_CRITICAL_KEYS
    mutator(state)
    post_critical = set(state.keys()) & _STATE_CRITICAL_KEYS
    # If the on-disk file existed and was non-empty AND we used to have at
    # least one critical key AND the post-mutation state has none of them,
    # something is wrong — refuse rather than persist a stripped state.
    try:
        on_disk_size = STATE_JSON.stat().st_size if STATE_JSON.exists() else 0
    except OSError:
        on_disk_size = 0
    if on_disk_size > 0 and pre_critical and not post_critical:
        _log("REFUSING state.json write: all critical keys would be wiped (bundle/linked_profiles/targets)")
        return False
    _write_atomic(STATE_JSON, state)
    return True


# ---------------------------------------------------------------------------
# Per-cycle ~/.claude.json snapshot (H1)
# ---------------------------------------------------------------------------


class ClaudeJsonContext:
    """One read of ~/.claude.json per cycle, shared across helpers.

    Each helper used to call _load_json(CLAUDE_JSON) independently — 8 reads
    per cycle, exposing the daemon to torn reads when Claude Code was writing
    concurrently. Now the poll loop loads once at the top of the cycle and
    passes this context into every helper that needs read access.

    Writes still re-read fresh (via _load_json) immediately before
    _write_atomic — by design — to avoid clobbering concurrent edits.
    """

    def __init__(self, data: dict | None, *, corrupt: bool = False):
        self._data: dict = data if data is not None else {}
        self.corrupt: bool = corrupt

    @classmethod
    def load(cls, path: Path = CLAUDE_JSON) -> "ClaudeJsonContext":
        try:
            data = _load_json_strict(path)
            return cls(data)
        except CorruptStateError as e:
            _log_corrupt(
                path, e.original, action="reading ~/.claude.json failed; helpers will see empty state this cycle"
            )
            return cls({}, corrupt=True)

    @property
    def data(self) -> dict:
        return self._data

    @property
    def mcp_servers(self) -> dict:
        return self._data.get("mcpServers", {}) or {}

    @property
    def projects(self) -> dict:
        return self._data.get("projects", {}) or {}


# Set at top of each poll cycle, cleared at end. Helpers consult this rather
# than re-reading ~/.claude.json. None means "no active snapshot, fall back
# to disk read" (test-friendly: helpers still work when called directly).
_CURRENT_CJ_SNAPSHOT: ClaudeJsonContext | None = None


def _cj_data() -> dict:
    """Return ~/.claude.json data — from the current cycle's snapshot if
    active, otherwise via a fresh permissive read."""
    if _CURRENT_CJ_SNAPSHOT is not None:
        return _CURRENT_CJ_SNAPSHOT.data
    return _load_json(CLAUDE_JSON)


def _daemon_running() -> int | None:
    """Return PID of running daemon if any, else None.

    Uses two independent signals so a stale/empty PID file doesn't fool us:

    1. Flock probe on the singleton lock file. If something else holds the
       exclusive flock, a daemon is alive even if the PID file is empty or
       missing. Returns the PID from the PID file (best effort) or -1 if
       unknown.
    2. PID file fallback (legacy). If flock probe is inconclusive, fall back
       to reading the PID file and signalling 0.
    """
    # Signal 1: flock probe — definitive
    try:
        SINGLETON_LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
        probe = open(SINGLETON_LOCK_FILE, "a+")  # noqa: SIM115
        try:
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            # We acquired it → no daemon running on this lock-file inode.
            fcntl.flock(probe, fcntl.LOCK_UN)
        except (BlockingIOError, OSError):
            # Someone holds it → a daemon is alive.
            try:
                pid = int(PID_FILE.read_text().strip()) if PID_FILE.exists() else -1
            except (ValueError, OSError):
                pid = -1
            probe.close()
            return pid
        finally:
            probe.close()
    except OSError:
        pass

    # Signal 2: PID file fallback (in case flock not supported on the FS)
    if not PID_FILE.exists():
        return None
    try:
        pid = int(PID_FILE.read_text().strip())
        os.kill(pid, 0)
        return pid
    except (ValueError, ProcessLookupError, PermissionError):
        PID_FILE.unlink(missing_ok=True)
        return None


# ---------------------------------------------------------------------------
# Source file discovery
# ---------------------------------------------------------------------------


def _collect_source_files(state: dict) -> dict[str, list[str]]:
    """Build a mapping of file_path -> [categories] for all watched files."""
    files: dict[str, list[str]] = {}

    def _add(path: Path, *categories: str):
        p = str(path)
        existing = files.setdefault(p, [])
        for cat in categories:
            if cat not in existing:
                existing.append(cat)

    # Base settings (affects everything)
    base = PROFILES_DIR / "_base" / "settings.base.json"
    if base.exists():
        _add(base, "base")

    # Built-in profiles
    if PROFILES_DIR.is_dir():
        for profile_dir in sorted(PROFILES_DIR.iterdir()):
            if not profile_dir.is_dir() or profile_dir.name.startswith("_"):
                continue
            pname = profile_dir.name
            cat = f"profile:{pname}"
            for candidate in [
                profile_dir / "profile.yml",
                profile_dir / ".claude" / "settings.overrides.json",
                profile_dir / "CLAUDE.md",
            ]:
                if candidate.exists():
                    _add(candidate, cat)

    # Bundle (profiles + connectors)
    bundle_info = state.get("bundle")
    if bundle_info:
        bundle_path = Path(bundle_info["path"])
        if bundle_path.is_dir():
            bp = bundle_path / "profiles"
            if bp.is_dir():
                for profile_dir in sorted(bp.iterdir()):
                    if not profile_dir.is_dir() or profile_dir.name.startswith("_"):
                        continue
                    pname = profile_dir.name
                    cat = f"profile:{pname}"
                    for candidate in [
                        profile_dir / "profile.yml",
                        profile_dir / ".claude" / "settings.overrides.json",
                        profile_dir / "CLAUDE.md",
                    ]:
                        if candidate.exists():
                            _add(candidate, cat, "bundle")

            bc = bundle_path / "connectors"
            if bc.is_dir():
                for conn_dir in sorted(bc.iterdir()):
                    if conn_dir.is_dir():
                        _add_connector_files(conn_dir, conn_dir.name, files, is_bundle=True)

    # Linked connectors from state.json
    for name, info in state.get("connectors", {}).items():
        conn_dir = Path(info["path"])
        if conn_dir.is_dir():
            _add_connector_files(conn_dir, name, files, is_bundle=False)

    # MCP files
    for mcp_path_str in state.get("mcpFiles", []):
        p = Path(mcp_path_str)
        if p.exists():
            _add(p, "mcp_files")

    # Bundle .claude/ asset directories (skills, agents, commands, rules, .mcp.json)
    # Use a manifest hash (sorted listing) so adds/removes trigger re-sync
    if bundle_info:
        bundle_path = Path(bundle_info["path"])
        bundle_claude = bundle_path / ".claude"
        if bundle_claude.is_dir():
            for subdir in ("skills", "agents", "commands", "rules"):
                d = bundle_claude / subdir
                if d.is_dir():
                    # Create a pseudo-file entry: hash the directory listing
                    manifest = str(d) + "/__manifest__"
                    files[manifest] = ["bundle"]
            mcp = bundle_claude / ".mcp.json"
            if mcp.exists():
                _add(mcp, "bundle")

        # Profile .claude/ asset directories
        active_profile = state.get("targets", {}).get("global", {}).get("profile")
        if active_profile:
            for search in [bundle_path / "profiles", PROFILES_DIR]:
                pd = search / active_profile / ".claude"
                if pd.is_dir():
                    for subdir in ("skills", "agents", "commands", "rules"):
                        d = pd / subdir
                        if d.is_dir():
                            manifest = str(d) + "/__manifest__"
                            files[manifest] = [f"profile:{active_profile}"]
                    mcp = pd / ".mcp.json"
                    if mcp.exists():
                        _add(mcp, f"profile:{active_profile}")

    # agentihooks built-in .claude/ asset directories
    ah_claude = AGENTIHOOKS_ROOT / ".claude"
    if ah_claude.is_dir():
        for subdir in ("skills", "agents", "commands", "rules"):
            d = ah_claude / subdir
            if d.is_dir():
                manifest = str(d) + "/__manifest__"
                files[manifest] = ["base"]

    # Env files
    env_main = AGENTIHOOKS_STATE_DIR / ".env"
    if env_main.exists():
        _add(env_main, "env")
    if AGENTIHOOKS_STATE_DIR.is_dir():
        for f in sorted(AGENTIHOOKS_STATE_DIR.iterdir()):
            if f.suffix == ".env" and f.is_file() and f != env_main:
                _add(f, "env")

    return files


def _add_connector_files(conn_dir: Path, name: str, files: dict[str, list[str]], *, is_bundle: bool) -> None:
    categories = [f"connector:{name}"]
    if is_bundle:
        categories.append("bundle")

    def _add(path: Path, *extra_cats: str):
        p = str(path)
        existing = files.setdefault(p, [])
        for cat in list(categories) + list(extra_cats):
            if cat not in existing:
                existing.append(cat)

    yml = conn_dir / "connector.yml"
    if yml.exists():
        _add(yml)

    profiles_dir = conn_dir / "profiles"
    if profiles_dir.is_dir():
        for profile_sub in sorted(profiles_dir.iterdir()):
            if not profile_sub.is_dir():
                continue
            for candidate_name in ["permissions.json", "env.json"]:
                candidate = profile_sub / candidate_name
                if candidate.exists():
                    _add(candidate)


# ---------------------------------------------------------------------------
# Hash comparison
# ---------------------------------------------------------------------------


_DIR_CONTENT_MAX_FILE_BYTES = 5 * 1024 * 1024  # 5 MB per-file cap (oversize → stat fallback)


def _dir_content_hash(dir_path: Path) -> str | None:
    """Hash directory contents recursively.

    Walks the tree, reads each file's SHA-256, and folds (relative_path, content_hash)
    pairs (sorted) into a single digest. Skips dotfiles, symlinks (to avoid loops),
    and files past the per-file cap (those contribute their stat size instead).

    Replaces the legacy filename-only manifest hash so edits to file BODIES inside
    watched directories actually trigger sync.
    """
    if not dir_path.is_dir():
        return None
    h = hashlib.sha256()
    entries: list[tuple[str, str]] = []
    try:
        for child in dir_path.rglob("*"):
            try:
                if child.is_symlink():
                    continue
                if any(part.startswith(".") for part in child.relative_to(dir_path).parts):
                    continue
                if not child.is_file():
                    continue
                rel = str(child.relative_to(dir_path))
                size = child.stat().st_size
                if size > _DIR_CONTENT_MAX_FILE_BYTES:
                    entries.append((rel, f"oversize:{size}"))
                    continue
                file_hash = _sha256(child)
                if file_hash is None:
                    entries.append((rel, "unreadable"))
                else:
                    entries.append((rel, file_hash))
            except OSError:
                continue
    except OSError:
        return None
    for rel, digest in sorted(entries):
        h.update(rel.encode("utf-8", errors="replace"))
        h.update(b"\0")
        h.update(digest.encode("ascii"))
        h.update(b"\n")
    return h.hexdigest()


def _compute_hashes(
    source_files: dict[str, list[str]],
    *,
    previous_hashes: dict[str, str] | None = None,
    failure_counts: dict[str, int] | None = None,
    failure_threshold: int = 3,
) -> dict[str, str]:
    """Compute hashes for every watched path.

    M1: When a file read fails transiently, carry the previous hash forward
    instead of dropping the path (which would re-appear as "added" next cycle
    and trigger a spurious reinstall). Only drop after `failure_threshold`
    consecutive failures. ``failure_counts`` is mutated in place.
    """
    result: dict[str, str] = {}
    previous_hashes = previous_hashes or {}
    failure_counts = failure_counts if failure_counts is not None else {}
    for path_str in source_files:
        if path_str.endswith("/__manifest__"):
            dir_path = Path(path_str.removesuffix("/__manifest__"))
            h = _dir_content_hash(dir_path)
        else:
            h = _sha256(Path(path_str))
        if h is not None:
            result[path_str] = h
            failure_counts.pop(path_str, None)
            continue
        # Read failed — count it.
        failure_counts[path_str] = failure_counts.get(path_str, 0) + 1
        prev = previous_hashes.get(path_str)
        if prev is not None and failure_counts[path_str] < failure_threshold:
            result[path_str] = prev  # carry forward, don't trigger spurious "removed"
    return result


def _load_hashes() -> dict[str, str]:
    data = _load_json(HASH_FILE)
    return data.get("hashes", {})


def _save_hashes(hashes: dict[str, str]) -> None:
    _write_atomic(
        HASH_FILE,
        {
            "_updated": datetime.now(timezone.utc).isoformat(),
            "hashes": hashes,
        },
    )


def _diff_hashes(old: dict[str, str], new: dict[str, str]) -> tuple[list[str], list[str], list[str]]:
    """Returns (changed, added, removed) file path lists."""
    all_keys = set(old) | set(new)
    changed, added, removed = [], [], []
    for k in sorted(all_keys):
        if k in old and k not in new:
            removed.append(k)
        elif k not in old and k in new:
            added.append(k)
        elif old.get(k) != new.get(k):
            changed.append(k)
    return changed, added, removed


# ---------------------------------------------------------------------------
# Propagation logic
# ---------------------------------------------------------------------------


def _determine_affected_categories(
    changed_files: list[str],
    source_file_map: dict[str, list[str]],
    *,
    removed_files: list[str] | None = None,
    old_source_map: dict[str, list[str]] | None = None,
) -> set[str]:
    categories: set[str] = set()
    for f in changed_files:
        categories.update(source_file_map.get(f, []))
    # Removed files: look up their categories from the old map if available,
    # otherwise treat any removal as a "base" change (safe over-propagation).
    for f in removed_files or []:
        if old_source_map and f in old_source_map:
            categories.update(old_source_map[f])
        else:
            categories.add("base")
    return categories


def _determine_actions(affected_categories: set[str], state: dict) -> dict:
    targets = state.get("targets", {})
    global_target = targets.get("global")
    project_targets = targets.get("projects", {})

    actions = {
        "reinstall_global": False,
        "reinstall_projects": [],
        "sync_mcp": False,
    }

    # base/bundle/env -> everything
    if affected_categories & {"base", "bundle", "env"}:
        if global_target:
            actions["reinstall_global"] = True
        actions["reinstall_projects"] = list(project_targets.keys())
        actions["sync_mcp"] = True
        return actions

    # mcp_files -> sync only
    if "mcp_files" in affected_categories:
        actions["sync_mcp"] = True

    # Profile changes -> targets using that profile
    for cat in affected_categories:
        if cat.startswith("profile:"):
            profile_name = cat.split(":", 1)[1]
            if global_target and global_target.get("profile") == profile_name:
                actions["reinstall_global"] = True
            for proj_path, proj_info in project_targets.items():
                if proj_info.get("profile") == profile_name:
                    if proj_path not in actions["reinstall_projects"]:
                        actions["reinstall_projects"].append(proj_path)

    # Connector changes -> targets whose profile is referenced by the connector.
    # M2: A connector with a `profiles/default/` directory affects every profile
    # (default fallback). Without `default`, the connector is scoped to whichever
    # explicit profile subdirs it ships. We only narrow the propagation when the
    # connector is explicitly scoped (no `default`) — otherwise we keep the
    # broad "reinstall all" behavior for safety.
    connector_cats = [c for c in affected_categories if c.startswith("connector:")]
    if connector_cats:
        affected_profiles: set[str] | None = set()
        for cat in connector_cats:
            name = cat.split(":", 1)[1]
            scoped = _connector_scoped_profiles(name, state)
            if scoped is None:
                affected_profiles = None  # connector affects everyone
                break
            affected_profiles.update(scoped)
        if affected_profiles is None:
            if global_target:
                actions["reinstall_global"] = True
            actions["reinstall_projects"] = list(project_targets.keys())
        else:
            if global_target and global_target.get("profile") in affected_profiles:
                actions["reinstall_global"] = True
            for proj_path, proj_info in project_targets.items():
                if proj_info.get("profile") in affected_profiles and proj_path not in actions["reinstall_projects"]:
                    actions["reinstall_projects"].append(proj_path)

    return actions


def _connector_scoped_profiles(name: str, state: dict) -> set[str] | None:
    """Return the set of profiles a connector affects, or None when the
    connector has a `profiles/default/` fallback (i.e. it affects every profile).

    Used by _determine_actions to avoid reinstalling projects whose profile
    is not actually touched by the changed connector.
    """
    info = (state.get("connectors") or {}).get(name)
    if not info:
        return None  # unknown connector — assume broadest scope
    raw_path = info.get("path") or ""
    if not raw_path:
        return None  # missing path — broadest scope (safer than guessing)
    conn_dir = Path(raw_path)
    profiles_root = conn_dir / "profiles"
    if not profiles_root.is_dir():
        return None
    has_default = (profiles_root / "default").is_dir()
    if has_default:
        return None  # default fallback means "all profiles"
    scoped = {p.name for p in profiles_root.iterdir() if p.is_dir()}
    return scoped or None


def _ensure_install_importable() -> None:
    """Add scripts/ to sys.path once so ``import install`` works."""
    scripts_dir = str(AGENTIHOOKS_ROOT / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)


def _execute_actions(actions: dict, state: dict) -> dict:
    summary = {
        "global_reinstalled": False,
        "projects_reinstalled": [],
        "mcp_synced": False,
        "errors": [],
    }

    # Acquire lock (non-blocking)
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    lock_fd = open(LOCK_FILE, "w")  # noqa: SIM115
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        _log("Lock held by another process — skipping this cycle")
        lock_fd.close()
        summary["errors"].append("lock_contention")
        return summary

    try:
        _ensure_install_importable()
        import install

        targets = state.get("targets", {})

        # Call _install_*_inner directly to avoid re-acquiring the same
        # flock from install_global/install_project wrappers (deadlock).
        if actions["reinstall_global"]:
            global_target = targets.get("global", {})
            profile = global_target.get("profile", "default")
            settings_profile = global_target.get("settings_profile", "")

            # Pre-flight: validate every entry in the chain still resolves on
            # disk. A transient git operation in the bundle (checkout, stash,
            # branch switch) can briefly remove profile files; reacting to
            # that by reinstalling would shrink the chain and clobber operator
            # intent. Skip the cycle and let the next sync pick up the
            # restored state.
            chain_names = [p.strip() for p in profile.split(",") if p.strip()]
            missing = []
            for pname in chain_names:
                try:
                    if install._resolve_profile_dir(pname) is None:
                        missing.append(pname)
                except Exception:
                    missing.append(pname)
            if missing:
                _log(
                    f"WARN: skipping global reinstall — chain entr{'y' if len(missing) == 1 else 'ies'} "
                    f"{missing} unresolvable on disk (likely transient — bundle git op?). "
                    f"Chain in state preserved: '{profile}'."
                )
                summary["errors"].append(f"global: skipped (missing: {','.join(missing)})")
            else:
                try:
                    _log(f"Re-installing global (profile={profile}, settings_profile={settings_profile or 'none'})")
                    ns = argparse.Namespace(profile=profile, settings_profile=settings_profile)
                    install._install_global_inner(ns)
                    summary["global_reinstalled"] = True
                    _log("Global re-install complete")
                except SystemExit as e:
                    if e.code and e.code != 0:
                        _log(f"ERROR re-installing global: exit code {e.code}")
                        summary["errors"].append(f"global: exit {e.code}")
                    else:
                        summary["global_reinstalled"] = True
                        _log("Global re-install complete (sys.exit caught)")
                except Exception as e:
                    _log(f"ERROR re-installing global: {e}")
                    summary["errors"].append(f"global: {e}")

        for proj_path in actions["reinstall_projects"]:
            p = Path(proj_path)
            if not p.exists():
                _log(f"WARN: Project path missing, skipping: {proj_path}")
                continue
            proj_info = targets.get("projects", {}).get(proj_path, {})
            profile = proj_info.get("profile", "default")
            try:
                _log(f"Syncing project settings {proj_path} (profile={profile})")
                # Read per-repo config if it exists, otherwise use profile only
                config_path = p / ".agentihooks.json"
                if config_path.exists():
                    config = json.loads(config_path.read_text())
                    config.setdefault("profile", profile)
                else:
                    config = {"profile": profile}
                # Only sync settings — do NOT create/overwrite .mcp.json
                install._write_project_settings(p, config)
                summary["projects_reinstalled"].append(proj_path)
                _log(f"Project settings sync complete: {proj_path}")
            except Exception as e:
                _log(f"ERROR syncing project {proj_path}: {e}")
                summary["errors"].append(f"project:{proj_path}: {e}")

        if actions["sync_mcp"]:
            try:
                _log("Syncing user MCP files")
                install.sync_user_mcp()
                summary["mcp_synced"] = True
                _log("MCP sync complete")
            except Exception as e:
                _log(f"ERROR syncing MCP: {e}")
                summary["errors"].append(f"mcp_sync: {e}")

    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()

    return summary


# ---------------------------------------------------------------------------
# MCP server tracking
# ---------------------------------------------------------------------------


def _get_valid_mcp_names() -> set[str]:
    """Return the set of MCP server names that actually exist right now.

    Sources of truth:
    1. ~/.claude.json top-level mcpServers (globally registered)
    2. .mcp.json files in registered project directories

    Excludes claude.ai web-session entries entirely.
    """
    data = _cj_data()  # H1: shared per-cycle snapshot
    # Global servers (exclude claude.ai web-session entries)
    valid = {name for name in data.get("mcpServers", {}).keys() if not name.startswith("claude.ai ")}
    # Project .mcp.json files
    for proj_path in data.get("projects", {}):
        mcp_file = Path(proj_path) / ".mcp.json"
        if mcp_file.exists():
            proj_mcp = _load_json(mcp_file)
            valid.update(proj_mcp.get("mcpServers", {}).keys())
    return valid


def _snapshot_project_enabled_mcps() -> None:
    """Read ~/.claude.json and record which MCPs each project has enabled into state.json.

    This runs at the start of every poll cycle so that user-enabled MCPs are
    captured before any write operations can overwrite them.
    Only updates projects already registered in state.json targets.
    """
    data = _cj_data()  # H1: shared per-cycle snapshot
    all_servers = set(data.get("mcpServers", {}).keys())
    all_servers = {s for s in all_servers if not s.startswith("claude.ai ")}
    if not all_servers:
        return

    projects = data.get("projects", {})
    if not projects:
        return

    # Routed through _safe_state_update so a corrupt/empty state.json cannot
    # cause this helper to merge into {} and blank bundle/linked_profiles.
    snapshot_logged = [False]

    def _mutate(state: dict) -> None:
        targets = state.get("targets", {})
        state_projects = targets.get("projects", {})
        if not state_projects:
            return
        changed = False
        for proj_path, proj_data in projects.items():
            if not isinstance(proj_data, dict):
                continue
            if proj_path not in state_projects:
                continue
            disabled = set(proj_data.get("disabledMcpServers", []))
            enabled = sorted(all_servers - disabled)
            prev = state_projects[proj_path].get("enabled_mcps")
            if prev != enabled:
                state_projects[proj_path]["enabled_mcps"] = enabled
                changed = True
        snapshot_logged[0] = changed

    if _safe_state_update(_mutate) and snapshot_logged[0]:
        _log("Snapshot: updated per-project enabled_mcps in state.json")


def _get_managed_mcp_names() -> set[str]:
    """Return server names that agentihooks sources currently define.

    Unlike _get_valid_mcp_names() (which reads ~/.claude.json as truth),
    this reads the actual source files: bundle .mcp.json, profile .mcp.json,
    hooks-utils, and state-tracked mcpFiles.
    """
    try:
        _ensure_install_importable()
        import install

        return set(install._collect_all_managed_mcp_servers().keys())
    except Exception as exc:
        _log(f"Warning: could not collect managed MCP names: {exc}")
        return set()


def _prune_stale_mcp_servers(known_servers_file: Path, *, verbose: bool = False) -> dict:
    """Remove stale MCP entries from all tracking locations.

    Prunes:
    0. Orphaned mcpServers in ~/.claude.json (managed by agentihooks but
       no longer defined in any source file)
    1. disabledMcpServers in every project entry in ~/.claude.json
    2. known-mcp-servers.json
    3. settings.local.json files in project .claude/ dirs

    Returns summary dict with counts.
    """
    valid = _get_valid_mcp_names()
    summary = {
        "pruned_disabled": 0,
        "pruned_known": 0,
        "pruned_settings": 0,
        "projects_touched": 0,
        "pruned_orphaned": 0,
    }

    if not valid:
        # M5: fall back to managed-by-source set so prune doesn't no-op forever
        # when ~/.claude.json is temporarily empty.
        managed_fallback = _get_managed_mcp_names()
        if managed_fallback:
            valid = managed_fallback
            _log(f"Prune: ~/.claude.json reports no MCP servers; falling back to managed set ({len(valid)} entries)")
        else:
            _log("Prune: no valid MCP servers found and no managed sources — skipping (safety)")
            return summary

    # 0. Remove orphaned mcpServers from ~/.claude.json
    #    A server is orphaned if it exists in ~/.claude.json mcpServers but is
    #    NOT defined in any agentihooks source (bundle, profile, mcpFiles, hooks-utils).
    #    We only remove servers that agentihooks could have added — skip anything
    #    that starts with "claude.ai " (web-session managed).
    managed = _get_managed_mcp_names()
    if managed:
        data = _load_json(CLAUDE_JSON)
        current_servers = data.get("mcpServers", {})
        orphaned = {name for name in current_servers if not name.startswith("claude.ai ") and name not in managed}
        if orphaned:
            for name in orphaned:
                del current_servers[name]
            _write_atomic(CLAUDE_JSON, data)
            summary["pruned_orphaned"] = len(orphaned)
            # Also remove from valid set so downstream prune steps don't keep them
            valid -= orphaned
            _log(f"Prune: removed {len(orphaned)} orphaned mcpServers from ~/.claude.json: {sorted(orphaned)}")
            if verbose:
                for name in sorted(orphaned):
                    _log(f"  Removed orphaned server: {name}")

    # 1. Prune disabledMcpServers in ~/.claude.json projects
    data = _load_json(CLAUDE_JSON)
    projects = data.get("projects", {})
    claude_json_dirty = False
    for proj_path, proj_data in projects.items():
        if not isinstance(proj_data, dict):
            continue
        disabled = proj_data.get("disabledMcpServers", [])
        if not disabled:
            continue
        cleaned = [s for s in disabled if s in valid]
        removed_count = len(disabled) - len(cleaned)
        if removed_count > 0:
            proj_data["disabledMcpServers"] = sorted(cleaned)
            summary["pruned_disabled"] += removed_count
            summary["projects_touched"] += 1
            claude_json_dirty = True
            if verbose:
                stale = set(disabled) - valid
                _log(f"  Pruned {removed_count} stale entries from {proj_path}: {sorted(stale)}")

    if claude_json_dirty:
        _write_atomic(CLAUDE_JSON, data)

    # 2. Prune known-mcp-servers.json
    known_data = _load_json(known_servers_file)
    known_list = set(known_data.get("knownMcpServers", []))
    stale_known = known_list - valid
    if stale_known:
        known_data["knownMcpServers"] = sorted(known_list - stale_known)
        _write_atomic(known_servers_file, known_data)
        summary["pruned_known"] = len(stale_known)
        if verbose:
            _log(f"  Pruned {len(stale_known)} from known-mcp-servers.json: {sorted(stale_known)}")

    # 3. Prune settings.local.json files in projects
    for proj_path in projects:
        settings_file = Path(proj_path) / ".claude" / "settings.local.json"
        if not settings_file.exists():
            continue
        settings = _load_json(settings_file)
        dirty = False
        for key in ("disabledMcpjsonServers", "enabledMcpjsonServers"):
            entries = settings.get(key, [])
            if not entries:
                continue
            # For settings.local.json, validate against that project's .mcp.json
            proj_mcp_file = Path(proj_path) / ".mcp.json"
            proj_mcp_names = set()
            if proj_mcp_file.exists():
                proj_mcp_names = set(_load_json(proj_mcp_file).get("mcpServers", {}).keys())
            # Also include global servers as valid
            all_valid_for_project = valid | proj_mcp_names
            cleaned = [s for s in entries if s in all_valid_for_project]
            removed = len(entries) - len(cleaned)
            if removed > 0:
                settings[key] = sorted(cleaned)
                dirty = True
                summary["pruned_settings"] += removed
                if verbose:
                    stale_entries = set(entries) - all_valid_for_project
                    _log(f"  Pruned {removed} from {settings_file}: {sorted(stale_entries)}")
        if dirty:
            _write_atomic(settings_file, settings)

    # 4. Prune enabled_mcps in state.json — routed through _safe_state_update
    # so a transient corrupt read cannot blank state.
    pruned_enabled_count = [0]

    def _mutate_prune(state: dict) -> None:
        state_projects = state.get("targets", {}).get("projects", {})
        for proj_path, proj_info in state_projects.items():
            enabled = proj_info.get("enabled_mcps", [])
            if not enabled:
                continue
            cleaned = [s for s in enabled if s in valid]
            removed = len(enabled) - len(cleaned)
            if removed > 0:
                proj_info["enabled_mcps"] = cleaned
                pruned_enabled_count[0] += removed

    _safe_state_update(_mutate_prune)
    summary["pruned_enabled"] = pruned_enabled_count[0]
    pruned_enabled = pruned_enabled_count[0]

    total = (
        summary["pruned_orphaned"]
        + summary["pruned_disabled"]
        + summary["pruned_known"]
        + summary["pruned_settings"]
        + pruned_enabled
    )
    if total > 0:
        _log(
            f"Prune: removed {total} stale MCP entries ({summary['pruned_orphaned']} orphaned, {summary['pruned_disabled']} disabled, {summary['pruned_known']} known, {summary['pruned_settings']} settings, {pruned_enabled} enabled)"
        )

    return summary


def _get_all_known_mcp_names_from_claude_json() -> set[str]:
    """Read local MCP server names from ~/.claude.json.

    Excludes claude.ai web-session entries — those are managed by claude.ai,
    not local config, and should not be tracked or blacklisted.
    """
    data = _cj_data()  # H1: shared per-cycle snapshot
    names = {name for name in data.get("mcpServers", {}).keys() if not name.startswith("claude.ai ")}
    return names


def _check_new_mcp_servers(known_servers_file: Path) -> None:
    """Detect new MCP servers and add them to disabledMcpServers in all projects."""
    current = _get_all_known_mcp_names_from_claude_json()
    if not current:
        return

    # Load previously known servers
    known_data = _load_json(known_servers_file)
    previously_known = set(known_data.get("knownMcpServers", []))

    if not previously_known:
        # First run — just save baseline, don't blacklist everything
        known_data["knownMcpServers"] = sorted(current)
        _write_atomic(known_servers_file, known_data)
        _log(f"MCP tracking: baseline set with {len(current)} servers")
        return

    new_servers = current - previously_known
    if not new_servers:
        return

    _log(f"MCP tracking: {len(new_servers)} new server(s): {sorted(new_servers)}")

    # Add new servers to disabledMcpServers in all project entries
    # Respect per-project and child-project whitelists
    _ensure_install_importable()
    import install

    data = _load_json(CLAUDE_JSON)
    projects = data.get("projects", {})
    all_project_paths = set(projects.keys())
    updated_count = 0
    for proj_path, proj_data in projects.items():
        existing_disabled = set(proj_data.get("disabledMcpServers", []))
        # Read own whitelist
        own_enabled: set[str] = set()
        proj_config = Path(proj_path) / ".agentihooks.json"
        if proj_config.exists():
            try:
                own_enabled = set(json.loads(proj_config.read_text()).get("enabledMcpServers", []))
            except (json.JSONDecodeError, OSError):
                pass
        child_enabled = install._collect_child_enabled_mcps(Path(proj_path), all_project_paths)
        protected = own_enabled | child_enabled
        to_add = new_servers - existing_disabled - protected
        if to_add:
            proj_data["disabledMcpServers"] = sorted(existing_disabled | to_add)
            updated_count += 1

    if updated_count:
        _write_atomic(CLAUDE_JSON, data)
        _log(
            f"MCP tracking: added {len(new_servers)} new server(s) to disabledMcpServers in {updated_count} project(s)"
        )

    # Update known servers
    known_data["knownMcpServers"] = sorted(current)
    _write_atomic(known_servers_file, known_data)


def _update_claude_json_snapshot() -> None:
    """Update the claude_json_snapshot in state.json with current ~/.claude.json metadata."""
    if not CLAUDE_JSON.exists():
        return

    data = _cj_data()  # H1: shared per-cycle snapshot
    if not data:
        return

    snapshot: dict = {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "mcp_servers": sorted(data.get("mcpServers", {}).keys()),
        "projects": {},
        "ever_connected": sorted(data.get("claudeAiMcpEverConnected", [])),
    }
    for path, proj in data.get("projects", {}).items():
        if not isinstance(proj, dict):
            continue
        disabled = proj.get("disabledMcpServers", [])
        snapshot["projects"][path] = {
            "disabled_mcp_count": len(disabled),
            "has_disabled_list": bool(disabled),
        }

    # Routed through _safe_state_update — see _safe_state_update docstring
    # for why permissive _load_json + _write_atomic was a bundle-strip risk.
    def _mutate_snapshot(state: dict) -> None:
        old_snapshot = state.get("claude_json_snapshot", {})

        old_projects = set(old_snapshot.get("projects", {}).keys())
        new_projects = set(snapshot["projects"].keys())
        added = new_projects - old_projects
        removed = old_projects - new_projects
        old_mcps = set(old_snapshot.get("mcp_servers", []))
        new_mcps = set(snapshot["mcp_servers"])
        new_servers = new_mcps - old_mcps
        gone_servers = old_mcps - new_mcps

        if added:
            _log(f"Snapshot: {len(added)} new project(s): {', '.join(sorted(added)[:5])}")
        if removed:
            _log(f"Snapshot: {len(removed)} removed project(s): {', '.join(sorted(removed)[:5])}")
        if new_servers:
            _log(f"Snapshot: {len(new_servers)} new MCP server(s): {', '.join(sorted(new_servers))}")
        if gone_servers:
            _log(f"Snapshot: {len(gone_servers)} removed MCP server(s): {', '.join(sorted(gone_servers))}")

        state["claude_json_snapshot"] = snapshot

    _safe_state_update(_mutate_snapshot)


# H2: known-projects ledger — first-seen timestamps for each project path.
# Daemon waits NEW_PROJECT_GRACE_SEC after first sight before backfilling
# disabledMcpServers, giving Claude Code a window to write its own preferences.
KNOWN_PROJECTS_FILE = AGENTIHOOKS_STATE_DIR / "known-projects.json"
NEW_PROJECT_GRACE_SEC = int(os.environ.get("AGENTIHOOKS_NEW_PROJECT_GRACE_SEC", "120"))


def _load_known_projects() -> dict[str, str]:
    """Map project_path -> first_seen ISO timestamp."""
    data = _load_json(KNOWN_PROJECTS_FILE)
    return data.get("projects", {}) if isinstance(data, dict) else {}


def _save_known_projects(known: dict[str, str]) -> None:
    _write_atomic(KNOWN_PROJECTS_FILE, {"_updated": datetime.now(timezone.utc).isoformat(), "projects": known})


def _check_new_projects(known_servers_file: Path) -> None:
    """Backfill disabledMcpServers for project entries that are missing it.

    Claude Code auto-creates project entries when opening new directories,
    but those entries have no disabledMcpServers — leaving all MCPs enabled.
    This function detects such entries and backfills the full blacklist.

    H2: For each project we track a first_seen timestamp in known-projects.json.
    On a project's first appearance the timestamp is recorded but no backfill
    is performed. Backfill only kicks in once now - first_seen exceeds the
    grace window (default 120s) — giving Claude Code time to write its own
    disabledMcpServers list before the daemon clobbers it.
    """
    if not CLAUDE_JSON.exists():
        return

    data = _load_json(CLAUDE_JSON)
    projects = data.get("projects", {})
    if not projects:
        return

    known_data = _load_json(known_servers_file)
    all_servers = set(known_data.get("knownMcpServers", []))
    if not all_servers:
        # Fall back to reading directly from ~/.claude.json
        all_servers = _get_all_known_mcp_names_from_claude_json()
    # Never backfill claude.ai web-session entries
    all_servers = {s for s in all_servers if not s.startswith("claude.ai ")}
    if not all_servers:
        return

    _ensure_install_importable()
    import install

    all_project_paths = set(projects.keys())
    backfilled = []

    # H2: load + update first-seen ledger
    known_projects = _load_known_projects()
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    ledger_dirty = False
    deferred = []

    for proj_path, proj_data in projects.items():
        if not isinstance(proj_data, dict):
            continue
        existing = proj_data.get("disabledMcpServers")
        if existing:
            continue  # has explicit prefs — leave alone
        # missing key or empty list → candidate for backfill
        first_seen_iso = known_projects.get(proj_path)
        if first_seen_iso is None:
            # first sighting — record + skip backfill this cycle
            known_projects[proj_path] = now_iso
            ledger_dirty = True
            deferred.append(proj_path)
            continue
        try:
            first_seen = datetime.fromisoformat(first_seen_iso)
        except ValueError:
            # corrupt timestamp — reset
            known_projects[proj_path] = now_iso
            ledger_dirty = True
            deferred.append(proj_path)
            continue
        age = (now - first_seen).total_seconds()
        if age < NEW_PROJECT_GRACE_SEC:
            deferred.append(proj_path)
            continue
        # past grace — backfill now
        own_enabled: set[str] = set()
        proj_config = Path(proj_path) / ".agentihooks.json"
        if proj_config.exists():
            try:
                own_enabled = set(json.loads(proj_config.read_text()).get("enabledMcpServers", []))
            except (json.JSONDecodeError, OSError):
                pass
        child_enabled = install._collect_child_enabled_mcps(Path(proj_path), all_project_paths)
        effective = sorted(all_servers - own_enabled - child_enabled)
        proj_data["disabledMcpServers"] = effective
        backfilled.append(proj_path)

    # Prune ledger of paths Claude Code has dropped
    if known_projects:
        stale_ledger = set(known_projects) - set(projects.keys())
        if stale_ledger:
            for p in stale_ledger:
                known_projects.pop(p, None)
            ledger_dirty = True

    if ledger_dirty:
        _save_known_projects(known_projects)

    if deferred:
        _log(f"New project grace window: deferring backfill for {len(deferred)} project(s)")

    if backfilled:
        _write_atomic(CLAUDE_JSON, data)
        for p in backfilled:
            _log(f"  Backfilled: {p}")
        _log(f"New project check: blacklisted MCPs in {len(backfilled)} new project(s)")


# ---------------------------------------------------------------------------
# Watchdog / heartbeat / crash-loop sentinel (M3 / M4 / M6)
# ---------------------------------------------------------------------------


HEARTBEAT_FILE = AGENTIHOOKS_STATE_DIR / "sync-daemon.heartbeat"
SENTINEL_FILE = AGENTIHOOKS_STATE_DIR / "sync-in-progress.json"
DEFAULT_STEP_TIMEOUT_SEC = int(os.environ.get("AGENTIHOOKS_SYNC_STEP_TIMEOUT_SEC", "30"))
MAX_CRASH_LOOP = 2


def _step(name: str, fn, timeout: float | None = DEFAULT_STEP_TIMEOUT_SEC, default=None):
    """M3: run *fn* with a soft timeout. If the worker thread is still alive
    past *timeout*, log a TIMEOUT line and return *default* — letting the
    cycle proceed instead of blocking forever on a stuck I/O step.

    Pass ``timeout=None`` to skip the watchdog entirely (use for legit
    long-running steps such as _execute_actions, where a 120s cap would
    falsely flag a real install as a stuck step).

    Caveat: Python threads cannot be force-killed. The worker continues in
    the background; the next cycle gets a fresh thread. Acceptable for
    I/O-bound steps (the common case here).
    """
    if timeout is None:
        try:
            return fn()
        except Exception as exc:
            _log(f"Step {name} raised: {exc}")
            return default

    import threading

    result: list = [default]
    error: list = [None]

    def _runner():
        try:
            result[0] = fn()
        except Exception as exc:  # surface inside the cycle, don't kill the daemon
            error[0] = exc

    t = threading.Thread(target=_runner, name=f"sync-step:{name}", daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        _log(f"STEP TIMEOUT after {timeout}s: {name} (abandoning, will retry next cycle)")
        return default
    if error[0] is not None:
        _log(f"Step {name} raised: {error[0]}")
        return default
    return result[0]


def _write_heartbeat(*, last_success_iso: str | None, cycles: int, failed_cycle_count: int = 0) -> None:
    """M4: persist a heartbeat so `agentihooks daemon status` can warn
    when the daemon is alive but not making progress.

    failed_cycle_count is persisted across restarts so the M6 crash-loop
    bound (MAX_CRASH_LOOP) is actually enforced — without this, the counter
    reset to 0 every restart and the guard never tripped.
    """
    try:
        version = _resolve_version()
    except Exception:
        version = "unknown"
    payload = {
        "last_cycle": datetime.now(timezone.utc).isoformat(),
        "last_success": last_success_iso,
        "cycles": cycles,
        "failed_cycle_count": failed_cycle_count,
        "version": version,
        "pid": os.getpid(),
    }
    try:
        _write_atomic(HEARTBEAT_FILE, payload)
    except OSError as exc:
        _log(f"Heartbeat write failed: {exc}")


def _resolve_version() -> str:
    pyproject = AGENTIHOOKS_ROOT / "pyproject.toml"
    if pyproject.exists():
        try:
            for line in pyproject.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith("version"):
                    parts = line.split("=", 1)
                    if len(parts) == 2:
                        return parts[1].strip().strip('"').strip("'")
        except OSError:
            pass
    return "unknown"


def _read_sentinel() -> dict | None:
    """M6: pre-execute sentinel — if present at startup, the previous
    cycle did not complete cleanly (SIGKILL/OOM/segfault between
    _execute_actions and _save_hashes)."""
    if not SENTINEL_FILE.exists():
        return None
    try:
        return json.loads(SENTINEL_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"corrupt": True}


def _write_sentinel(actions: dict, cycle_id: int) -> None:
    payload = {
        "cycle_id": cycle_id,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "attempted_actions": {
            "reinstall_global": actions.get("reinstall_global", False),
            "reinstall_projects": actions.get("reinstall_projects", []),
            "sync_mcp": actions.get("sync_mcp", False),
        },
    }
    try:
        _write_atomic(SENTINEL_FILE, payload)
    except OSError as exc:
        _log(f"Sentinel write failed: {exc}")


def _clear_sentinel() -> None:
    try:
        SENTINEL_FILE.unlink(missing_ok=True)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Main daemon loop
# ---------------------------------------------------------------------------


def _run_daemon(poll_sec: int) -> None:
    # Singleton enforcement via flock on a DEDICATED lock file (not the PID file).
    # The PID file gets deleted by `init`, `--force`, manual cleanup, daemon stop,
    # etc. — flock is per-inode, so deleting the PID file would orphan the flock
    # and let the next daemon coexist. The singleton lock file lives at a stable
    # dotfile path that no install/force/clean code path touches.
    SINGLETON_LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    lock_fd = open(SINGLETON_LOCK_FILE, "a+")  # noqa: SIM115 — held for life
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        try:
            existing = PID_FILE.read_text().strip() if PID_FILE.exists() else "?"
        except OSError:
            existing = "?"
        lock_fd.close()
        _log(f"Sync daemon already running (PID {existing}) — exiting")
        print(f"[sync] Daemon already running (PID {existing}) — exiting", flush=True)
        return

    # Capture the lock-file inode so we can detect deletion-and-recreate
    # (someone unlinks the lock file → next start gets a fresh inode and would
    # silently coexist with us; we self-exit on next tick to prevent that).
    try:
        _lock_inode = os.fstat(lock_fd.fileno()).st_ino
    except OSError:
        _lock_inode = -1

    # Write PID to the (separate) PID file — informational only
    try:
        PID_FILE.write_text(str(os.getpid()))
    except OSError as e:
        _log(f"Could not write PID file: {e}")

    _log(f"Sync daemon started (poll={poll_sec}s, pid={os.getpid()}, lock_inode={_lock_inode})")

    running = True

    def _handle_signal(signum, frame):
        nonlocal running
        _log(f"Received signal {signum} — shutting down")
        running = False

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    # M6: detect a previous-cycle crash via stale sentinel.
    sentinel = _read_sentinel()
    crash_loop_count = 0
    if sentinel is not None:
        _log("PREVIOUS CYCLE DID NOT COMPLETE CLEANLY")
        if sentinel.get("corrupt"):
            _log("  sentinel file present but unreadable")
        else:
            _log(f"  cycle_id={sentinel.get('cycle_id')}, started_at={sentinel.get('started_at')}")
            _log(f"  attempted_actions={sentinel.get('attempted_actions')}")
        try:
            hb = _load_json(HEARTBEAT_FILE)
            crash_loop_count = int(hb.get("failed_cycle_count", 0)) + 1
        except (ValueError, OSError):
            crash_loop_count = 1
        _log(f"  consecutive failed cycles: {crash_loop_count}")
        _clear_sentinel()

    # Baseline
    state = _load_json(STATE_JSON)
    source_files = _collect_source_files(state)
    failure_counts: dict[str, int] = {}
    current_hashes = _compute_hashes(source_files, previous_hashes={}, failure_counts=failure_counts)
    old_hashes = _load_hashes()

    if not old_hashes:
        _log(f"First run — baseline hashes for {len(source_files)} files")
        _save_hashes(current_hashes)
        old_hashes = current_hashes
    else:
        changed, added, removed = _diff_hashes(old_hashes, current_hashes)
        if changed or added or removed:
            _log(f"Changes since last run: {len(changed)} changed, {len(added)} added, {len(removed)} removed")
            all_changed = changed + added
            if all_changed or removed:
                affected = _determine_affected_categories(
                    all_changed,
                    source_files,
                    removed_files=removed,
                )
                actions = _determine_actions(affected, state)
                if any([actions["reinstall_global"], actions["reinstall_projects"], actions["sync_mcp"]]):
                    _log(f"Affected categories: {sorted(affected)}")
                    _execute_actions(actions, state)
            _save_hashes(current_hashes)
            old_hashes = current_hashes

    old_source_map = dict(source_files)  # track for removed-file lookups
    _log(f"Watching {len(source_files)} source files")
    targets = state.get("targets", {})
    if targets.get("global"):
        _log(f"  Global: {targets['global']['path']} (profile: {targets['global']['profile']})")
    projects = targets.get("projects", {})
    for p, info in projects.items():
        _log(f"  Project: {p} (profile: {info['profile']})")
    if not targets:
        _log("  WARNING: No targets registered. Run 'agentihooks global' first.")

    # Poll loop
    cycle_counter = 0
    last_success_iso: str | None = None
    global _CURRENT_CJ_SNAPSHOT
    while running:
        time.sleep(poll_sec)
        if not running:
            break

        # Self-check: if our singleton lock file's inode changed (someone
        # deleted+recreated the file), exit cleanly. A new daemon may have
        # already grabbed the new inode; staying alive would mean two
        # daemons coexisting on different inodes — the original sprawl bug.
        try:
            current_inode = os.stat(SINGLETON_LOCK_FILE).st_ino
            if _lock_inode != -1 and current_inode != _lock_inode:
                _log(
                    f"Singleton lock inode changed ({_lock_inode} -> {current_inode}); "
                    "another daemon may have taken over. Exiting."
                )
                running = False
                break
        except OSError:
            # Lock file missing — same risk; exit and let the next start re-establish.
            _log("Singleton lock file vanished; exiting to avoid sprawl.")
            running = False
            break

        cycle_counter += 1
        cycle_had_critical_error = False

        # H1: load ~/.claude.json once for the whole cycle.
        _CURRENT_CJ_SNAPSHOT = ClaudeJsonContext.load()
        try:
            try:
                state = _load_json_strict(STATE_JSON)
            except CorruptStateError as e:
                _log_corrupt(e.path, e.original)
                cycle_had_critical_error = True
                continue  # skip the rest of this cycle

            source_files = _step("collect_source_files", lambda: _collect_source_files(state), default={})
            new_hashes = _step(
                "compute_hashes",
                lambda: _compute_hashes(
                    source_files,
                    previous_hashes=old_hashes,
                    failure_counts=failure_counts,
                ),
                default=dict(old_hashes),  # carry forward on timeout
            )
            changed, added, removed = _diff_hashes(old_hashes, new_hashes)

            if changed or added or removed:
                for f in changed:
                    _log(f"  CHANGED: {f}")
                for f in added:
                    _log(f"  ADDED: {f}")
                for f in removed:
                    _log(f"  REMOVED: {f}")

                all_changed = changed + added
                hash_advance_blocked = False
                if all_changed or removed:
                    affected = _determine_affected_categories(
                        all_changed,
                        source_files,
                        removed_files=removed,
                        old_source_map=old_source_map,
                    )
                    _log(f"Affected categories: {sorted(affected)}")
                    actions = _determine_actions(affected, state)

                    action_desc = []
                    if actions["reinstall_global"]:
                        action_desc.append("global")
                    if actions["reinstall_projects"]:
                        action_desc.append(f"{len(actions['reinstall_projects'])} project(s)")
                    if actions["sync_mcp"]:
                        action_desc.append("mcp_sync")

                    if action_desc:
                        _log(f"Actions: {', '.join(action_desc)}")
                        # M6: drop a sentinel before the install — if the daemon
                        # is killed mid-execute, the next start sees this and
                        # bumps the failed-cycle counter.
                        if crash_loop_count >= MAX_CRASH_LOOP:
                            _log(
                                f"CRITICAL: {crash_loop_count} consecutive failed cycles on the "
                                "same change. Advancing hashes and skipping execute. Operator must intervene."
                            )
                            cycle_had_critical_error = True
                        else:
                            _write_sentinel(actions, cycle_counter)
                            summary = _step(
                                "execute_actions",
                                lambda: _execute_actions(actions, state),
                                timeout=None,  # installs are legit long; no watchdog here
                                default={"errors": ["timeout"]},
                            ) or {"errors": ["timeout"]}
                            errors = summary.get("errors") or []
                            if "lock_contention" in errors:
                                # C2: a concurrent agentihooks init holds the lock.
                                # DO NOT advance hashes — retry on next cycle.
                                _log("Hash advance DEFERRED (lock_contention); will retry next cycle")
                                hash_advance_blocked = True
                            elif errors:
                                _log(f"Completed with {len(errors)} error(s) — advancing hashes (RETRY-WONT-HELP)")
                                cycle_had_critical_error = True
                            else:
                                _log("All actions completed successfully")
                            _clear_sentinel()
                    else:
                        _log("No actions needed (no matching targets)")

                if not hash_advance_blocked:
                    _save_hashes(new_hashes)
                    old_hashes = new_hashes
                    old_source_map = dict(source_files)

            # Snapshot per-project enabled MCPs BEFORE any writes
            _step("snapshot_enabled_mcps", _snapshot_project_enabled_mcps)

            # Always check for new MCP servers and new projects (every cycle)
            known_servers_file = AGENTIHOOKS_STATE_DIR / "known-mcp-servers.json"
            _step("check_new_mcp_servers", lambda: _check_new_mcp_servers(known_servers_file))
            _step("check_new_projects", lambda: _check_new_projects(known_servers_file))

            def _sessions_heartbeat():
                from hooks.context.broadcast import heartbeat_sessions

                hb = heartbeat_sessions()
                if hb.get("flipped_dead") or hb.get("pruned"):
                    _log(
                        f"Sessions heartbeat: alive={hb['alive']} "
                        f"dead={hb['flipped_dead']} pruned={hb['pruned']} "
                        f"total={hb['total']}"
                    )

            _step("sessions_heartbeat", _sessions_heartbeat)
            _step("update_claude_json_snapshot", _update_claude_json_snapshot)
            # Prune LAST — after all additions, so we don't fight with backfill
            _step("prune_stale_mcp_servers", lambda: _prune_stale_mcp_servers(known_servers_file))

            # Clean up stale broadcast sessions (dead PIDs)
            try:
                from hooks.context.broadcast import get_active_sessions

                get_active_sessions(cleanup=True)
            except Exception:
                pass  # broadcast module may not be importable in all envs

            # Memory mirror tick — authority-only in v5. Consumer + contributor
            # roles are driven by Claude Code hooks (UserPromptSubmit /
            # PostToolUse / Stop), not wall-clock polling. The daemon still
            # owns authority because authority memory can mutate outside any
            # Claude session (operator editing files directly in vim /
            # Obsidian on the laptop).
            try:
                from hooks import config as _cfg

                _mm_role = (getattr(_cfg, "MEMORY_MIRROR_ROLE", None) or _cfg.MEMORY_MIRROR_MODE or "off").lower()
                if _mm_role == "authority" and (_cfg.MEMORY_MIRROR_REMOTE or "").strip():
                    from scripts import memory_mirror_sync

                    memory_mirror_sync.tick()
            except Exception as mm_err:
                _log(f"Memory mirror tick error: {mm_err}")

        except Exception as e:
            _log(f"ERROR in poll cycle: {e}")
            import traceback

            traceback.print_exc()
            cycle_had_critical_error = True
        finally:
            # H1: clear the per-cycle snapshot so subsequent fresh reads use disk.
            _CURRENT_CJ_SNAPSHOT = None
            # M4: heartbeat regardless of success — but only update last_success
            # when the cycle was clean.
            if not cycle_had_critical_error:
                last_success_iso = datetime.now(timezone.utc).isoformat()
                crash_loop_count = 0
            _write_heartbeat(
                last_success_iso=last_success_iso,
                cycles=cycle_counter,
                failed_cycle_count=crash_loop_count,
            )

    _log("Sync daemon stopped")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
    except OSError:
        pass
    lock_fd.close()
    PID_FILE.unlink(missing_ok=True)
    # Note: SINGLETON_LOCK_FILE itself is intentionally NOT unlinked —
    # keeping the inode stable across daemon restarts is the whole point.


# ---------------------------------------------------------------------------
# Daemon lifecycle
# ---------------------------------------------------------------------------


def _start_daemon(poll: int = DEFAULT_POLL_SEC) -> None:
    existing = _daemon_running()
    if existing:
        print(f"[sync] Daemon already running (PID {existing}).", flush=True)
        print("  Logs: agentihooks daemon logs", flush=True)
        return

    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

    python = sys.executable
    daemon_script = str(Path(__file__).resolve())

    log_fd = open(LOG_FILE, "a")  # noqa: SIM115
    proc = subprocess.Popen(
        [python, daemon_script, "--foreground", "--poll", str(poll)],
        stdout=log_fd,
        stderr=log_fd,
        start_new_session=True,
    )
    log_fd.close()  # child inherits the fd; parent no longer needs it
    PID_FILE.write_text(str(proc.pid))
    print(f"[sync] Daemon started (PID {proc.pid}).", flush=True)
    print("  Logs:   agentihooks daemon logs", flush=True)
    print("  Status: agentihooks daemon status", flush=True)
    print("  Stop:   agentihooks daemon stop", flush=True)


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------


def main():
    ap = argparse.ArgumentParser(description="agentihooks sync daemon")
    ap.add_argument(
        "--poll",
        type=int,
        default=int(os.getenv("AGENTIHOOKS_SYNC_POLL_SEC", str(DEFAULT_POLL_SEC))),
    )
    ap.add_argument("--foreground", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()

    if args.foreground:
        _run_daemon(args.poll)
    else:
        _start_daemon(poll=args.poll)


if __name__ == "__main__":
    main()
