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
            settings_profile = global_target.get("settings_profile", "")
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
    data = _load_json(CLAUDE_JSON)
    # Global servers (exclude claude.ai web-session entries)
    valid = {
        name for name in data.get("mcpServers", {}).keys()
        if not name.startswith("claude.ai ")
    }
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
    data = _load_json(CLAUDE_JSON)
    all_servers = set(data.get("mcpServers", {}).keys())
    all_servers = {s for s in all_servers if not s.startswith("claude.ai ")}
    if not all_servers:
        return

    projects = data.get("projects", {})
    if not projects:
        return

    state = _load_json(STATE_JSON)
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

    if changed:
        _write_atomic(STATE_JSON, state)
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
    summary = {"pruned_disabled": 0, "pruned_known": 0, "pruned_settings": 0, "projects_touched": 0, "pruned_orphaned": 0}

    if not valid:
        _log("Prune: no valid MCP servers found — skipping (safety)")
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
        orphaned = {
            name for name in current_servers
            if not name.startswith("claude.ai ") and name not in managed
        }
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

    # 4. Prune enabled_mcps in state.json — remove servers that no longer exist
    state = _load_json(STATE_JSON)
    state_projects = state.get("targets", {}).get("projects", {})
    state_dirty = False
    pruned_enabled = 0
    for proj_path, proj_info in state_projects.items():
        enabled = proj_info.get("enabled_mcps", [])
        if not enabled:
            continue
        cleaned = [s for s in enabled if s in valid]
        removed = len(enabled) - len(cleaned)
        if removed > 0:
            proj_info["enabled_mcps"] = cleaned
            pruned_enabled += removed
            state_dirty = True
    if state_dirty:
        _write_atomic(STATE_JSON, state)
    summary["pruned_enabled"] = pruned_enabled

    total = summary["pruned_orphaned"] + summary["pruned_disabled"] + summary["pruned_known"] + summary["pruned_settings"] + pruned_enabled
    if total > 0:
        _log(f"Prune: removed {total} stale MCP entries ({summary['pruned_orphaned']} orphaned, {summary['pruned_disabled']} disabled, {summary['pruned_known']} known, {summary['pruned_settings']} settings, {pruned_enabled} enabled)")

    return summary


def _get_all_known_mcp_names_from_claude_json() -> set[str]:
    """Read local MCP server names from ~/.claude.json.

    Excludes claude.ai web-session entries — those are managed by claude.ai,
    not local config, and should not be tracked or blacklisted.
    """
    data = _load_json(CLAUDE_JSON)
    names = {
        name for name in data.get("mcpServers", {}).keys()
        if not name.startswith("claude.ai ")
    }
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

    data = _load_json(CLAUDE_JSON)
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

    state = _load_json(STATE_JSON)
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
    _write_atomic(STATE_JSON, state)


def _check_new_projects(known_servers_file: Path) -> None:
    """Backfill disabledMcpServers for project entries that are missing it.

    Claude Code auto-creates project entries when opening new directories,
    but those entries have no disabledMcpServers — leaving all MCPs enabled.
    This function detects such entries and backfills the full blacklist.
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

    for proj_path, proj_data in projects.items():
        if not isinstance(proj_data, dict):
            continue
        existing = proj_data.get("disabledMcpServers")
        if not existing:  # missing key or empty list
            # Respect own and child whitelists when backfilling
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

    if backfilled:
        _write_atomic(CLAUDE_JSON, data)
        for p in backfilled:
            _log(f"  Backfilled: {p}")
        _log(f"New project check: blacklisted MCPs in {len(backfilled)} new project(s)")


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
    while running:
        time.sleep(poll_sec)
        if not running:
            break

        try:
            state = _load_json(STATE_JSON)
            source_files = _collect_source_files(state)
            new_hashes = _compute_hashes(source_files)
            changed, added, removed = _diff_hashes(old_hashes, new_hashes)

            if changed or added or removed:
                for f in changed:
                    _log(f"  CHANGED: {f}")
                for f in added:
                    _log(f"  ADDED: {f}")
                for f in removed:
                    _log(f"  REMOVED: {f}")

                all_changed = changed + added
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

            # Snapshot per-project enabled MCPs BEFORE any writes
            try:
                _snapshot_project_enabled_mcps()
            except Exception as snap_mcps_err:
                _log(f"Enabled MCP snapshot error: {snap_mcps_err}")

            # Always check for new MCP servers and new projects (every cycle)
            known_servers_file = AGENTIHOOKS_STATE_DIR / "known-mcp-servers.json"
            try:
                _check_new_mcp_servers(known_servers_file)
            except Exception as mcp_err:
                _log(f"MCP tracking error: {mcp_err}")
            try:
                _check_new_projects(known_servers_file)
            except Exception as proj_err:
                _log(f"New project check error: {proj_err}")
            try:
                _update_claude_json_snapshot()
            except Exception as snap_err:
                _log(f"Snapshot update error: {snap_err}")
            # Prune LAST — after all additions, so we don't fight with backfill
            try:
                _prune_stale_mcp_servers(known_servers_file)
            except Exception as prune_err:
                _log(f"MCP prune error: {prune_err}")

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
