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
AGENTIHOOKS_STATE_DIR = Path.home() / ".agentihooks"
STATE_JSON = AGENTIHOOKS_STATE_DIR / "state.json"
HASH_FILE = AGENTIHOOKS_STATE_DIR / "sync-hashes.json"
PID_FILE = AGENTIHOOKS_STATE_DIR / "sync-daemon.pid"
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


def _load_json(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _daemon_running() -> int | None:
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


def _add_connector_files(
    conn_dir: Path, name: str, files: dict[str, list[str]], *, is_bundle: bool
) -> None:
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


def _dir_manifest_hash(dir_path: Path) -> str | None:
    """Hash a sorted listing of directory children (names only)."""
    if not dir_path.is_dir():
        return None
    import hashlib
    names = sorted(p.name for p in dir_path.iterdir() if not p.name.startswith("."))
    return hashlib.sha256("\n".join(names).encode()).hexdigest()


def _compute_hashes(source_files: dict[str, list[str]]) -> dict[str, str]:
    result = {}
    for path_str in source_files:
        if path_str.endswith("/__manifest__"):
            dir_path = Path(path_str.removesuffix("/__manifest__"))
            h = _dir_manifest_hash(dir_path)
        else:
            h = _sha256(Path(path_str))
        if h is not None:
            result[path_str] = h
    return result


def _load_hashes() -> dict[str, str]:
    data = _load_json(HASH_FILE)
    return data.get("hashes", {})


def _save_hashes(hashes: dict[str, str]) -> None:
    _write_atomic(HASH_FILE, {
        "_updated": datetime.now(timezone.utc).isoformat(),
        "hashes": hashes,
    })


def _diff_hashes(
    old: dict[str, str], new: dict[str, str]
) -> tuple[list[str], list[str], list[str]]:
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

    # Connector changes -> all targets
    for cat in affected_categories:
        if cat.startswith("connector:"):
            if global_target:
                actions["reinstall_global"] = True
            actions["reinstall_projects"] = list(project_targets.keys())
            break

    return actions


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
            try:
                _log(f"Re-installing global (profile={profile})")
                ns = argparse.Namespace(profile=profile)
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
                _log(f"Re-installing project {proj_path} (profile={profile})")
                ns = argparse.Namespace(profile=profile, path=proj_path)
                # Bypass interactive confirmation for overwrites
                import builtins
                original_input = builtins.input
                builtins.input = lambda *a, **kw: "y"
                try:
                    install._install_project_inner(ns)
                finally:
                    builtins.input = original_input
                summary["projects_reinstalled"].append(proj_path)
                _log(f"Project re-install complete: {proj_path}")
            except SystemExit as e:
                if e.code and e.code != 0:
                    _log(f"ERROR re-installing project {proj_path}: exit code {e.code}")
                    summary["errors"].append(f"project:{proj_path}: exit {e.code}")
                else:
                    summary["projects_reinstalled"].append(proj_path)
                    _log(f"Project re-install complete: {proj_path} (sys.exit caught)")
            except Exception as e:
                _log(f"ERROR re-installing project {proj_path}: {e}")
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
# MCP server tracking — additive-only blacklisting
# ---------------------------------------------------------------------------


def _get_all_known_mcp_names_from_claude_json() -> set[str]:
    """Read ALL MCP server names from ~/.claude.json."""
    data = _load_json(CLAUDE_JSON)
    names = set(data.get("mcpServers", {}).keys())
    names.update(data.get("claudeAiMcpEverConnected", []))
    return names


def _check_new_mcp_servers(known_servers_file: Path) -> None:
    """Detect new MCP servers and add them to disabledMcpServers in all projects.

    Additive-only: never removes from disabledMcpServers (preserves UI toggles).
    """
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
    data = _load_json(CLAUDE_JSON)
    projects = data.get("projects", {})
    updated_count = 0
    for proj_path, proj_data in projects.items():
        existing_disabled = set(proj_data.get("disabledMcpServers", []))
        to_add = new_servers - existing_disabled
        if to_add:
            proj_data["disabledMcpServers"] = sorted(existing_disabled | to_add)
            updated_count += 1

    if updated_count:
        _write_atomic(CLAUDE_JSON, data)
        _log(f"MCP tracking: added {len(new_servers)} new server(s) to disabledMcpServers in {updated_count} project(s)")

    # Update known servers
    known_data["knownMcpServers"] = sorted(current)
    _write_atomic(known_servers_file, known_data)


# ---------------------------------------------------------------------------
# Main daemon loop
# ---------------------------------------------------------------------------


def _run_daemon(poll_sec: int) -> None:
    _log(f"Sync daemon started (poll={poll_sec}s, pid={os.getpid()})")

    running = True

    def _handle_signal(signum, frame):
        nonlocal running
        _log(f"Received signal {signum} — shutting down")
        running = False

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    # Baseline
    state = _load_json(STATE_JSON)
    source_files = _collect_source_files(state)
    current_hashes = _compute_hashes(source_files)
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
                    all_changed, source_files, removed_files=removed,
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
    while running:
        time.sleep(poll_sec)
        if not running:
            break

        try:
            state = _load_json(STATE_JSON)
            source_files = _collect_source_files(state)
            new_hashes = _compute_hashes(source_files)
            changed, added, removed = _diff_hashes(old_hashes, new_hashes)

            if not changed and not added and not removed:
                continue

            for f in changed:
                _log(f"  CHANGED: {f}")
            for f in added:
                _log(f"  ADDED: {f}")
            for f in removed:
                _log(f"  REMOVED: {f}")

            all_changed = changed + added
            if all_changed or removed:
                # For removed files, look up categories from the old source map
                affected = _determine_affected_categories(
                    all_changed, source_files,
                    removed_files=removed, old_source_map=old_source_map,
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
                    summary = _execute_actions(actions, state)
                    if summary["errors"]:
                        _log(f"Completed with {len(summary['errors'])} error(s)")
                    else:
                        _log("All actions completed successfully")
                else:
                    _log("No actions needed (no matching targets)")

            _save_hashes(new_hashes)
            old_hashes = new_hashes
            old_source_map = dict(source_files)

            # Check for new MCP servers (additive blacklisting)
            try:
                _check_new_mcp_servers(AGENTIHOOKS_STATE_DIR / "known-mcp-servers.json")
            except Exception as mcp_err:
                _log(f"MCP tracking error: {mcp_err}")

        except Exception as e:
            _log(f"ERROR in poll cycle: {e}")
            import traceback
            traceback.print_exc()

    _log("Sync daemon stopped")
    PID_FILE.unlink(missing_ok=True)


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
