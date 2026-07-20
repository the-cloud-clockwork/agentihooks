#!/usr/bin/env python3
# If accidentally run with bash, re-exec with python3 (polyglot trick).
""":'
exec python3 "$0" "$@"
exit
"""

# NOTE: The triple-quoted polyglot string above becomes __doc__ in Python,
# so the real usage text is stored in _USAGE_TEXT and used as the argparse epilog.
_USAGE_TEXT = """
Quick start:
    agentihooks init --bundle /path/to/bundle --profile colt
    agentihooks init --profile colt          # re-run (bundle already linked)

Commands:
    agentihooks init [--bundle PATH] [--profile NAME]
        First-time setup or re-install. Links a bundle, selects a profile,
        and installs everything into ~/.claude: hooks, settings, skills,
        agents, commands, rules, CLAUDE.md, and MCP servers.

    agentihooks uninstall [--yes]
        Remove all agentihooks artifacts: symlinks, settings, CLAUDE.md,
        MCP servers, and the CLI. Preserves ~/.agentihooks/state.json.

    agentihooks quota [auth|status|stop|logs]
        Manage the Claude.ai console quota watcher.

    agentihooks prune [-v]
        Remove stale MCP entries from disabledMcpServers and known-mcp-servers.json.

    agentihooks ignore [path] [--force]
        Create a .claudeignore in the current directory.

    agentihooks claude [extra flags]
        Launch claude with profile flags (model, permissions, effort, etc.)
        Alias: agenti (added to ~/.bashrc by init)

    agentihooks --list-profiles     # show available profiles
    agentihooks --query             # print active profile name

Profile layout (mirrors Claude Code project structure):
    profiles/<name>/
    ├── CLAUDE.md                    # system prompt (→ ~/.claude/CLAUDE.md)
    └── .claude/
        ├── settings.overrides.json  # merged into ~/.claude/settings.json
        ├── .mcp.json                # profile MCP servers
        ├── skills/                  # → ~/.claude/skills/
        ├── agents/                  # → ~/.claude/agents/
        ├── commands/                # → ~/.claude/commands/
        └── rules/                   # → ~/.claude/rules/

3-layer merge: agentihooks built-in → bundle global → profile-specific.
All commands are idempotent. Data directory: ~/.agentihooks/
"""

import argparse
import contextlib
import fcntl
import json
import os
import re
import shutil
import sys
from collections.abc import Callable
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

import yaml


def _get_version() -> str:
    """Read agentihooks version from importlib or pyproject.toml."""
    try:
        from importlib.metadata import version

        return version("agentihooks")
    except Exception:
        pass
    toml = Path(__file__).resolve().parent.parent / "pyproject.toml"
    if toml.exists():
        for line in toml.read_text().splitlines():
            if line.startswith("version"):
                return line.split('"')[1]
    return "unknown"


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

AGENTIHOOKS_ROOT = Path(__file__).resolve().parent.parent


def _is_source_checkout() -> bool:
    """True when agentihooks is running from a source tree (has pyproject.toml).

    False when installed as a wheel into site-packages (PyPI install).
    """
    return (AGENTIHOOKS_ROOT / "pyproject.toml").exists()


PROFILES_DIR = AGENTIHOOKS_ROOT / "profiles"
BASE_SETTINGS = PROFILES_DIR / "_base" / "settings.base.json"


def _resolve_claude_home() -> Path:
    """Resolve the Claude config directory.

    Priority: CLAUDE_CODE_HOME_DIR > AGENTIHOOKS_CLAUDE_HOME > ~/.claude

    CLAUDE_CODE_HOME_DIR points at the home-dir root (like $HOME);
    we append .claude automatically.
    AGENTIHOOKS_CLAUDE_HOME points directly at the .claude directory (legacy).
    """
    home_dir = os.environ.get("CLAUDE_CODE_HOME_DIR")
    if home_dir:
        return Path(home_dir) / ".claude"

    claude_home = os.environ.get("AGENTIHOOKS_CLAUDE_HOME")
    if claude_home:
        return Path(claude_home)

    return Path.home() / ".claude"


CLAUDE_HOME = _resolve_claude_home()

# Persistent state directory for user-level agentihooks configuration.
# Overridable via AGENTIHOOKS_HOME for per-pod isolation on shared filesystems.
AGENTIHOOKS_STATE_DIR = Path(os.environ.get("AGENTIHOOKS_HOME", str(Path.home() / ".agentihooks")))
STATE_JSON = AGENTIHOOKS_STATE_DIR / "state.json"

# Repeated path fragment constants (avoids S1192 duplicate-literal warnings)
_CLAUDE_SUBDIR = ".claude"
_CLAUDE_MD_NAME = "CLAUDE.md"
_MCP_JSON_NAME = ".mcp.json"

# Keys from ~/.claude/settings.json that belong to the user and should be
# preserved when merging (unless the base settings already define them).
PERSONAL_KEYS = {"model", "autoUpdatesChannel", "skipDangerousModePermissionPrompt"}

# Marker written into the managed settings so we can detect re-runs.
MANAGED_BY_KEY = "_managedBy"
MANAGED_BY_VALUE = "agentihooks/scripts/install.py"

# ANSI colors for terminal output
_BOLD = "\033[1m"
_DIM = "\033[2m"
_GREEN = "\033[32m"
_YELLOW = "\033[33m"
_RED = "\033[31m"
_CYAN = "\033[36m"
_RESET = "\033[0m"

_TAG_COLORS = {
    "[OK]": _GREEN,
    "[--]": _DIM,
    "[!!]": _YELLOW,
    "[RM]": _RED,
    "[WARN]": _YELLOW,
}


def _cprint(msg: str, **kwargs) -> None:
    """Print with auto-colored status tags."""
    for tag, color in _TAG_COLORS.items():
        if tag in msg:
            print(f"{color}{msg}{_RESET}", **kwargs)
            return
    print(msg, **kwargs)


# ---------------------------------------------------------------------------
# .claudeignore template
# ---------------------------------------------------------------------------

CLAUDEIGNORE_TEMPLATE = """\
# .claudeignore — files and directories Claude Code will not read or index.
# Generated by: agentihooks ignore
# Syntax: same as .gitignore (glob patterns, one per line).

# ── Credentials & secrets ──────────────────────────────────────────────────
.env
.env.*
!.env.example
*.pem
*.key
*.p12
*.pfx
*.jks
*.keystore
secrets/
credentials/
.secrets/

# ── Build artefacts ────────────────────────────────────────────────────────
__pycache__/
*.py[cod]
*.pyo
.mypy_cache/
.ruff_cache/
.pytest_cache/
dist/
build/
*.egg-info/
.eggs/
node_modules/
.next/
.nuxt/
out/
target/           # Rust / Java
*.class

# ── Runtime & generated data ───────────────────────────────────────────────
*.log
*.log.*
logs/
*.sqlite
*.sqlite3
*.db
*.lock             # e.g. poetry.lock, package-lock.json — often huge
!pyproject.toml    # keep build manifests readable

# ── Test coverage & reports ────────────────────────────────────────────────
.coverage
htmlcov/
coverage.xml
*.lcov
junit*.xml

# ── IDE & OS noise ─────────────────────────────────────────────────────────
.idea/
.vscode/
*.swp
*.swo
.DS_Store
Thumbs.db

# ── Large binary / media ───────────────────────────────────────────────────
*.zip
*.tar.gz
*.tgz
*.tar.bz2
*.gz
*.rar
*.7z
*.iso
*.dmg
*.pdf
*.png
*.jpg
*.jpeg
*.gif
*.svg
*.ico
*.mp4
*.mp3
*.wav
*.woff
*.woff2
*.ttf
*.eot

# ── Virtual environments ───────────────────────────────────────────────────
.venv/
venv/
env/
.env/

# ── Terraform / Ansible ────────────────────────────────────────────────────
.terraform/
*.tfstate
*.tfstate.backup
.terraform.lock.hcl
"""


# ---------------------------------------------------------------------------
# JSON helpers
# ---------------------------------------------------------------------------


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _deep_merge(base: dict, override: dict, _parent_key: str = "") -> dict:
    """Recursively merge *override* into *base*.

    Merge rules:
    - Dicts: recursive key-by-key merge (additive, non-destructive)
    - Arrays under ``hooks.*``: **append** (base hooks stay, profile hooks added)
    - All other values: override wins (last layer takes precedence)
    """
    merged = deepcopy(base)
    for key, value in override.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = _deep_merge(merged[key], value, _parent_key=key)
        elif _parent_key == "hooks" and key in merged and isinstance(merged[key], list) and isinstance(value, list):
            # Hook arrays: append profile hooks after base hooks
            merged[key] = deepcopy(merged[key]) + deepcopy(value)
        else:
            merged[key] = deepcopy(value)
    return merged


# ---------------------------------------------------------------------------
# State helpers (~/.agentihooks/state.json)
# ---------------------------------------------------------------------------


def _load_state() -> dict:
    if STATE_JSON.exists():
        try:
            return load_json(STATE_JSON)
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_state(state: dict) -> None:
    AGENTIHOOKS_STATE_DIR.mkdir(parents=True, exist_ok=True)
    # Keep one generation of backup so a bad init can always be forensically
    # reconstructed / restored (state.json used to be overwritten blind — a
    # profile-losing init left no trace of what the previous install was).
    if STATE_JSON.exists():
        try:
            shutil.copy2(STATE_JSON, STATE_JSON.with_suffix(".json.bak"))
        except OSError:
            pass  # backup is best-effort; never block the install on it
    save_json(STATE_JSON, state)


def _migrate_profile_rename(state: dict, old_name: str, new_name: str) -> None:
    """One-shot migration: rewrite all profile references from old_name to new_name in state.json."""
    changed = False
    targets = state.get("targets", {})

    # Global target
    g = targets.get("global", {})
    if g.get("profile") == old_name:
        g["profile"] = new_name
        changed = True

    # Per-project targets
    for _proj_key, proj in targets.get("projects", {}).items():
        if proj.get("profile") == old_name:
            proj["profile"] = new_name
            changed = True

    if changed:
        _save_state(state)
        print(f"  {_GREEN}[OK] Migrated profile '{old_name}' → '{new_name}' in state.json{_RESET}")

    # Also migrate ~/.claude.json project entries that reference the old profile
    claude_json = Path.home() / ".claude.json"
    if claude_json.exists():
        try:
            cj = json.loads(claude_json.read_text())
            cj_changed = False
            for _path, proj_cfg in cj.get("projects", {}).items():
                if isinstance(proj_cfg, dict) and proj_cfg.get("profile") == old_name:
                    proj_cfg["profile"] = new_name
                    cj_changed = True
            if cj_changed:
                claude_json.write_text(json.dumps(cj, indent=2))
                print(f"  {_GREEN}[OK] Migrated profile '{old_name}' → '{new_name}' in ~/.claude.json{_RESET}")
        except Exception:
            pass


def _state_add_mcp(mcp_path: Path) -> None:
    """Record *mcp_path* in state.json so --sync can restore it."""
    state = _load_state()
    paths: list[str] = state.get("mcpFiles", [])
    path_str = str(mcp_path)
    if path_str not in paths:
        paths.append(path_str)
        state["mcpFiles"] = paths
        _save_state(state)


def _state_set_mcp_lib(path: Path) -> None:
    """Persist the MCP library directory in state.json."""
    state = _load_state()
    state["mcpLibPath"] = str(path)
    _save_state(state)


def _state_get_mcp_lib() -> Path | None:
    """Return the saved MCP library directory, or None if not set."""
    val = _load_state().get("mcpLibPath")
    return Path(val) if val else None


def _state_remove_mcp(mcp_path: Path) -> None:
    """Remove *mcp_path* from state.json."""
    state = _load_state()
    paths: list[str] = state.get("mcpFiles", [])
    path_str = str(mcp_path)
    if path_str in paths:
        paths.remove(path_str)
        state["mcpFiles"] = paths
        _save_state(state)


# ---------------------------------------------------------------------------
# Target registry (state.json)
# ---------------------------------------------------------------------------

_SYNC_LOCK_FILE = AGENTIHOOKS_STATE_DIR / "sync.lock"


def _register_target_global(profile: str, settings_profile: str = "") -> None:
    """Record the global install in state.json."""
    state = _load_state()
    targets = state.setdefault("targets", {})
    entry = {
        "path": str(CLAUDE_HOME),
        "profile": profile,
        "installed_at": datetime.now(timezone.utc).isoformat(),
    }
    if settings_profile:
        entry["settings_profile"] = settings_profile
    targets["global"] = entry
    _save_state(state)


def _register_target_project(project_path: Path, profile: str) -> None:
    """Record a project install in state.json."""
    state = _load_state()
    targets = state.setdefault("targets", {})
    projects = targets.setdefault("projects", {})
    projects[str(project_path)] = {
        "profile": profile,
        "installed_at": datetime.now(timezone.utc).isoformat(),
    }
    _save_state(state)


def _unregister_target_project(project_path: Path) -> None:
    """Remove a project from state.json targets."""
    state = _load_state()
    targets = state.get("targets", {})
    projects = targets.get("projects", {})
    projects.pop(str(project_path), None)
    _save_state(state)


@contextlib.contextmanager
def _sync_lock(*, blocking: bool = True):
    """Advisory file lock for install operations.

    Default ``blocking=True`` waits for any concurrent install to finish.
    """
    _SYNC_LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd = open(_SYNC_LOCK_FILE, "w")  # noqa: SIM115
    try:
        flags = fcntl.LOCK_EX if blocking else (fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, flags)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        fd.close()


# ---------------------------------------------------------------------------
# Bundle helpers
# ---------------------------------------------------------------------------


def _print_bundle_discover_hint(*, profile_hint: str = "default") -> None:
    """When init runs without a linked bundle and without --bundle, suggest
    the env-var escape hatch the operator can use to register one. Read-only —
    does NOT mutate state. Skipped when --no-discover is set.

    Only $AGENTIHOOKS_BUNDLE_PATH is consulted; bundle directories can be named
    anything, so guessing by directory name is unreliable.
    """
    env = os.environ.get("AGENTIHOOKS_BUNDLE_PATH", "").strip()
    if env:
        cand = Path(env).expanduser()
        try:
            cand_str = str(cand.resolve())
        except OSError:
            cand_str = str(cand)
        if cand.is_dir() and (cand / "profiles").is_dir():
            print(f"{_DIM}    [hint] $AGENTIHOOKS_BUNDLE_PATH points to a valid bundle: {cand_str}{_RESET}")
            print(
                f"{_DIM}           re-link with: agentihooks init --bundle {cand_str} --profile {profile_hint}{_RESET}"
            )
            return
    print(f"{_DIM}    [hint] re-link a bundle with: agentihooks init --bundle <path> --profile <name>{_RESET}")


def _get_bundle_path() -> Path | None:
    """Return the linked bundle path from state.json, or AGENTIHOOKS_BUNDLE_PATH env var."""
    state = _load_state()
    bundle = state.get("bundle")
    if bundle:
        p = Path(bundle["path"])
        if p.is_dir():
            return p
    # Fallback: auto-discover from env var (headless/container mode)
    env_path = os.environ.get("AGENTIHOOKS_BUNDLE_PATH", "")
    if env_path:
        p = Path(env_path).expanduser().resolve()
        if p.is_dir():
            return p
    return None


def _get_linked_profiles() -> list[dict]:
    """Return the list of externally linked profiles from state.json."""
    state = _load_state()
    entries = state.get("linked_profiles", [])
    if not isinstance(entries, list):
        return []
    return entries


def _resolve_linked_profile_dir(profile_name: str) -> Path | None:
    """Resolve a profile name from the linked_profiles registry."""
    for entry in _get_linked_profiles():
        if entry.get("name") == profile_name:
            p = Path(entry.get("path", ""))
            if p.is_dir():
                return p
            return None
    return None


def _resolve_profile_dir(profile_name: str) -> Path | None:
    """Resolve a profile name to its directory — built-in, bundle, then linked."""
    # Built-in profiles
    local = PROFILES_DIR / profile_name
    if local.is_dir():
        return local
    # Bundle profiles
    bundle = _get_bundle_path()
    if bundle:
        bp = bundle / "profiles" / profile_name
        if bp.is_dir():
            return bp
    # Linked external profiles
    linked = _resolve_linked_profile_dir(profile_name)
    if linked is not None:
        return linked
    return None


def _profile_source_label(profile_name: str) -> str:
    """Return 'built-in', 'bundle', or 'linked' for a profile name."""
    if (PROFILES_DIR / profile_name).is_dir():
        return "built-in"
    bundle = _get_bundle_path()
    if bundle and (bundle / "profiles" / profile_name).is_dir():
        return "bundle"
    if _resolve_linked_profile_dir(profile_name) is not None:
        return "linked"
    return "unknown"


def _resolve_profile_chain(profile_input: str) -> list[tuple[str, Path]]:
    """Resolve a comma-separated profile chain to a list of (name, path) tuples.

    Returns an empty list if any profile in the chain cannot be resolved.
    """
    chain = [p.strip() for p in profile_input.split(",") if p.strip()]
    if not chain:
        return []
    dirs: list[tuple[str, Path]] = []
    linked_names = {e.get("name") for e in _get_linked_profiles()}
    for name in chain:
        d = _resolve_profile_dir(name)
        if d is None:
            if name in linked_names:
                _cprint(
                    f"  [WARN] Linked profile '{name}' path is missing — "
                    f"run 'agentihooks link-profile unlink {name}' to clean up. Skipping."
                )
            else:
                _cprint(f"  [WARN] Profile '{name}' in chain '{profile_input}' not found — skipping.")
            continue
        dirs.append((name, d))
    return dirs


def cmd_bundle(action: str, path: str | None = None, rebase: bool = False) -> None:
    """Handle 'agentihooks bundle' subcommands."""
    if action == "link":
        _bundle_link(Path(path).expanduser().resolve() if path else None)
    elif action == "unlink":
        _bundle_unlink()
    elif action == "list":
        _bundle_list()
    elif action == "pull":
        _bundle_pull(rebase=rebase)
    else:
        print(f"Unknown bundle action: {action}", file=sys.stderr)
        sys.exit(1)


def _bundle_pull(rebase: bool = False) -> None:
    """Git pull the linked bundle directory."""
    import subprocess as _sp

    bundle_dir = _get_bundle_path()
    if not bundle_dir:
        print("No bundle linked.", file=sys.stderr)
        sys.exit(1)

    if not (bundle_dir / ".git").exists():
        print(f"ERROR: {bundle_dir} is not a git repository.", file=sys.stderr)
        sys.exit(1)

    cmd = ["git", "-C", str(bundle_dir), "pull"]
    if rebase:
        cmd.append("--rebase")

    print(f"Pulling bundle: {bundle_dir}")
    print(f"  $ {' '.join(cmd)}")
    result = _sp.run(cmd, capture_output=True, text=True)

    if result.stdout.strip():
        print(result.stdout.strip())
    if result.stderr.strip():
        print(result.stderr.strip())

    if result.returncode != 0:
        print("ERROR: git pull failed.", file=sys.stderr)
        sys.exit(1)

    _cprint("[OK] Bundle updated.")


def _bundle_link(bundle_dir: Path | None) -> None:
    """Link a bundle directory."""
    if bundle_dir is None:
        print("ERROR: Provide the path to a bundle directory.", file=sys.stderr)
        sys.exit(1)

    if not bundle_dir.is_dir():
        print(f"ERROR: {bundle_dir} is not a directory.", file=sys.stderr)
        sys.exit(1)

    state = _load_state()
    old_bundle = state.get("bundle")
    if old_bundle:
        print(f"Replacing existing bundle: {old_bundle['path']}")

    state["bundle"] = {
        "path": str(bundle_dir),
        "linked_at": datetime.now(timezone.utc).isoformat(),
    }

    _save_state(state)

    # Summary
    profiles_dir = bundle_dir / "profiles"
    profile_names = sorted(p.name for p in profiles_dir.iterdir() if p.is_dir()) if profiles_dir.is_dir() else []
    has_claude = (bundle_dir / ".claude").is_dir()

    _cprint(f"[OK] Linked bundle: {bundle_dir}")
    if profile_names:
        print(f"     Profiles: {', '.join(profile_names)}")
    if has_claude:
        print("     Global .claude/: skills, commands, agents")
    print()
    print("Run 'agentihooks init' to apply.")


def _bundle_unlink() -> None:
    """Unlink the current bundle."""
    state = _load_state()
    bundle = state.get("bundle")
    if not bundle:
        print("No bundle linked.")
        return

    bundle_path = bundle["path"]

    # Remove bundle-sourced connectors
    connectors = state.get("connectors", {})
    to_remove = [name for name, info in connectors.items() if info.get("source") == "bundle"]
    for name in to_remove:
        del connectors[name]
        _cprint(f"  [OK] Removed bundle connector: {name}")

    del state["bundle"]
    _save_state(state)
    _cprint(f"[OK] Unlinked bundle: {bundle_path}")
    print()
    print("Run 'agentihooks init' to remove bundle settings.")


def _bundle_list() -> None:
    """Show the linked bundle and its contents."""
    bundle = _get_bundle_path()
    if not bundle:
        print("No bundle linked.")
        print()
        print("Link one with:")
        print("  agentihooks bundle link /path/to/.agentihooks")
        return

    state = _load_state()
    linked_at = state.get("bundle", {}).get("linked_at", "?")

    print(f"Bundle: {bundle}")
    print(f"Linked: {linked_at}")
    print()

    # Profiles
    profiles_dir = bundle / "profiles"
    if profiles_dir.is_dir():
        profile_names = sorted(p.name for p in profiles_dir.iterdir() if p.is_dir())
        if profile_names:
            print(f"Profiles ({len(profile_names)}):")
            for name in profile_names:
                print(f"  {name}")
            print()

    # Connectors
    conn_dir = bundle / "connectors"
    if conn_dir.is_dir():
        conn_names = sorted(d.name for d in conn_dir.iterdir() if d.is_dir() and (d / "connector.yml").exists())
        if conn_names:
            print(f"Connectors ({len(conn_names)}):")
            for name in conn_names:
                print(f"  {name}")
            print()


# ---------------------------------------------------------------------------
# Linked profile helpers (external dirs added to the chain)
# ---------------------------------------------------------------------------


def cmd_link_profile(
    action: str,
    path: str | None = None,
    name: str | None = None,
    *,
    no_append: bool = False,
    no_init: bool = False,
) -> None:
    """Handle 'agentihooks link-profile' subcommands.

    State mutation runs under ``_sync_lock``. ``install_global`` is invoked
    *outside* the lock to avoid a same-process flock deadlock (it acquires
    its own ``_sync_lock``).
    """
    if action == "link":
        with _sync_lock():
            install_args = _link_profile_link(
                Path(path).expanduser().resolve() if path else None,
                name=name,
                append=not no_append,
                run_init=not no_init,
            )
        if install_args is not None:
            print()
            install_global(install_args)
    elif action == "unlink":
        with _sync_lock():
            install_args = _link_profile_unlink(name, run_init=not no_init)
        if install_args is not None:
            print()
            install_global(install_args)
    elif action == "list":
        _link_profile_list()
    else:
        print(f"Unknown link-profile action: {action}", file=sys.stderr)
        sys.exit(1)


def _link_profile_link(
    profile_dir: Path | None,
    *,
    name: str | None = None,
    append: bool = True,
    run_init: bool = True,
) -> argparse.Namespace | None:
    """Register an external directory as a chain-able profile.

    Returns the ``install_global`` argparse namespace when the caller should
    re-run install (so it can be invoked outside the lock), or ``None`` if
    no install is needed.
    """
    if profile_dir is None:
        print("ERROR: Provide the path to a profile directory.", file=sys.stderr)
        sys.exit(1)

    if not profile_dir.is_dir():
        print(f"ERROR: {profile_dir} is not a directory.", file=sys.stderr)
        sys.exit(1)

    derived_name = (name or profile_dir.name).strip()
    if not derived_name:
        print("ERROR: Could not derive profile name. Use --name <alias>.", file=sys.stderr)
        sys.exit(1)

    # Collision guard: built-in or bundle profile of same name
    if (PROFILES_DIR / derived_name).is_dir():
        print(
            f"ERROR: Name '{derived_name}' collides with a built-in profile. Pass --name <alias> to disambiguate.",
            file=sys.stderr,
        )
        sys.exit(1)
    bundle = _get_bundle_path()
    if bundle and (bundle / "profiles" / derived_name).is_dir():
        print(
            f"ERROR: Name '{derived_name}' collides with a bundle profile. Pass --name <alias> to disambiguate.",
            file=sys.stderr,
        )
        sys.exit(1)

    state = _load_state()
    entries = state.get("linked_profiles", [])
    if not isinstance(entries, list):
        entries = []

    # Upsert
    existing = next((e for e in entries if e.get("name") == derived_name), None)
    if existing:
        old_path = existing.get("path", "")
        if old_path != str(profile_dir):
            print(f"Replacing linked profile '{derived_name}': {old_path} -> {profile_dir}")
        existing["path"] = str(profile_dir)
        existing["linked_at"] = datetime.now(timezone.utc).isoformat()
    else:
        entries.append(
            {
                "name": derived_name,
                "path": str(profile_dir),
                "linked_at": datetime.now(timezone.utc).isoformat(),
            }
        )
    state["linked_profiles"] = entries

    # Append to global chain (idempotent)
    global_target = state.setdefault("targets", {}).setdefault("global", {})
    current_chain_str = global_target.get("profile", "")
    chain_after = [p.strip() for p in current_chain_str.split(",") if p.strip()]
    chain_changed = False
    if append:
        if derived_name not in chain_after:
            chain_after.append(derived_name)
            chain_changed = True
        new_chain_str = ",".join(chain_after) if chain_after else derived_name
        global_target["profile"] = new_chain_str
    else:
        new_chain_str = current_chain_str

    settings_profile = global_target.get("settings_profile", "")

    if not run_init and append and chain_changed:
        # No follow-up install will refresh installed_at — bump it here so the
        # next manual `agentihooks init` sees the chain change.
        global_target["installed_at"] = datetime.now(timezone.utc).isoformat()

    _save_state(state)

    _cprint(f"[OK] Linked profile: {derived_name} -> {profile_dir}")
    if append and chain_changed:
        print(f"     Chain: {new_chain_str}")
    elif append and not chain_changed:
        print(f"     Chain unchanged: {new_chain_str}")

    if run_init and append and new_chain_str:
        return argparse.Namespace(profile=new_chain_str, settings_profile=settings_profile)
    if not run_init:
        print()
        print("Run 'agentihooks init' to apply.")
    return None


def _sweep_symlinks_into(target_root: Path) -> None:
    """Remove any symlinks under ~/.claude/{rules,agents,commands,skills} whose target sits under *target_root*.

    Uses ``os.readlink`` on the raw target string so dangling symlinks (whose
    target was deleted along with the linked profile) are still detected and
    removed.
    """
    target_str = str(target_root).rstrip("/")
    try:
        target_resolved = str(target_root.resolve()).rstrip("/")
    except OSError:
        target_resolved = target_str
    for subdir in ("rules", "agents", "commands", "skills"):
        d = CLAUDE_HOME / subdir
        if not d.is_dir():
            continue
        for link in sorted(d.iterdir()):
            if not link.is_symlink():
                continue
            try:
                raw_target = os.readlink(link)
            except OSError:
                continue
            # Match either the raw symlink target or its resolved form.
            raw = raw_target.rstrip("/")
            matches = (
                raw == target_str
                or raw == target_resolved
                or raw.startswith(target_str + "/")
                or raw.startswith(target_resolved + "/")
            )
            if not matches:
                # Resolve symlink chain only when the raw target wasn't decisive.
                try:
                    resolved = str(link.resolve()).rstrip("/")
                except OSError:
                    resolved = ""
                if resolved and (resolved == target_resolved or resolved.startswith(target_resolved + "/")):
                    matches = True
            if not matches:
                continue
            link.unlink()
            _cprint(f"  [RM] Removed orphan symlink: {subdir}/{link.name}")


def _link_profile_unlink(name: str | None, *, run_init: bool = True) -> argparse.Namespace | None:
    """Remove a linked profile from the registry and the global chain.

    Returns the ``install_global`` argparse namespace when the caller should
    re-run install, or ``None`` if no install is needed.
    """
    if not name:
        print("ERROR: Provide the linked profile name.", file=sys.stderr)
        sys.exit(1)

    state = _load_state()
    entries = state.get("linked_profiles", [])
    if not isinstance(entries, list):
        entries = []

    entry = next((e for e in entries if e.get("name") == name), None)
    if not entry:
        print(f"ERROR: No linked profile named '{name}'.", file=sys.stderr)
        sys.exit(1)

    entries = [e for e in entries if e.get("name") != name]
    state["linked_profiles"] = entries

    # Strip from chain
    global_target = state.setdefault("targets", {}).setdefault("global", {})
    chain = [p.strip() for p in global_target.get("profile", "").split(",") if p.strip()]
    was_in_chain = name in chain
    chain = [p for p in chain if p != name]
    if chain:
        new_chain_str = ",".join(chain)
    else:
        # Fall back to first available profile (default if it exists, else
        # whatever's there). Never leave the chain string empty.
        avail = _available_profiles()
        new_chain_str = "default" if "default" in avail else (avail[0] if avail else "default")
    global_target["profile"] = new_chain_str
    settings_profile = global_target.get("settings_profile", "")

    if not run_init and was_in_chain:
        global_target["installed_at"] = datetime.now(timezone.utc).isoformat()

    _save_state(state)

    _cprint(f"[OK] Unlinked profile: {name} (path was {entry.get('path', '?')})")
    if was_in_chain:
        print(f"     Chain: {new_chain_str}")
    else:
        print("     Was not in chain — registry entry removed only.")

    # Sweep orphan symlinks pointing into the unlinked path before re-install
    old_path = entry.get("path", "")
    if old_path:
        _sweep_symlinks_into(Path(old_path))

    if run_init and was_in_chain:
        return argparse.Namespace(profile=new_chain_str, settings_profile=settings_profile)
    if not run_init:
        print()
        print("Run 'agentihooks init' to apply.")
    return None


def _link_profile_list() -> None:
    """Show all externally linked profiles."""
    entries = _get_linked_profiles()
    if not entries:
        print("No linked profiles.")
        print()
        print("Link one with:")
        print("  agentihooks link-profile <path> [--name <alias>]")
        return

    state = _load_state()
    chain = [p.strip() for p in state.get("targets", {}).get("global", {}).get("profile", "").split(",") if p.strip()]

    print(f"Linked profiles ({len(entries)}):")
    for e in entries:
        n = e.get("name", "?")
        p = e.get("path", "?")
        linked_at = e.get("linked_at", "?")
        in_chain = " [in chain]" if n in chain else ""
        exists = "" if Path(p).is_dir() else " [MISSING]"
        print(f"  {n}{in_chain}{exists}")
        print(f"    path:   {p}")
        print(f"    linked: {linked_at}")


# ---------------------------------------------------------------------------
# Connector helpers
# ---------------------------------------------------------------------------


def _load_connectors(profile_name: str) -> tuple[dict, list[str], list[str]]:
    """Load linked connectors and return merged (env_dict, deny_list, disabled_servers) for *profile_name*.

    Connectors are external directories registered in state.json that provide
    per-profile permissions.deny rules, disabledMcpjsonServers, and env var
    overrides. They are additive only — they append rules and merge env vars.
    """
    state = _load_state()
    connectors = state.get("connectors", {})
    if not connectors:
        return {}, [], []

    merged_env: dict[str, str] = {}
    merged_deny: list[str] = []
    merged_disabled_servers: list[str] = []

    for name, info in connectors.items():
        conn_dir = Path(info["path"])
        conn_yml = conn_dir / "connector.yml"
        if not conn_yml.exists():
            _cprint(f"  [WARN] Connector '{name}' at {conn_dir} missing connector.yml — skipping")
            continue
        if not conn_dir.is_dir():
            _cprint(f"  [WARN] Connector '{name}' path {conn_dir} not found — skipping")
            continue

        try:
            meta = yaml.safe_load(conn_yml.read_text())
        except Exception as exc:
            _cprint(f"  [WARN] Connector '{name}' connector.yml parse error: {exc} — skipping")
            continue

        # Base env (applied to all profiles)
        base_env = (meta.get("base") or {}).get("env", {})
        merged_env.update(base_env)

        # Profile-specific settings (fall back to "default" if exact profile missing)
        profile_dir = conn_dir / "profiles" / profile_name
        if not profile_dir.is_dir():
            fallback = conn_dir / "profiles" / "default"
            if fallback.is_dir():
                _cprint(f"  [--] Connector '{name}': no profile '{profile_name}', falling back to 'default'")
                profile_dir = fallback
        if profile_dir.is_dir():
            perms_file = profile_dir / "permissions.json"
            if perms_file.exists():
                try:
                    perms = json.loads(perms_file.read_text())
                    merged_deny.extend(perms.get("deny", []))
                    merged_disabled_servers.extend(perms.get("disabledMcpjsonServers", []))
                except (json.JSONDecodeError, OSError) as exc:
                    _cprint(f"  [WARN] Connector '{name}' permissions.json error: {exc}")

            env_file = profile_dir / "env.json"
            if env_file.exists():
                try:
                    env_data = json.loads(env_file.read_text())
                    merged_env.update(env_data)
                except (json.JSONDecodeError, OSError) as exc:
                    _cprint(f"  [WARN] Connector '{name}' env.json error: {exc}")

    return merged_env, merged_deny, merged_disabled_servers


def cmd_connector(action: str, path: str | None = None, name: str | None = None, **kwargs) -> None:
    """Handle 'agentihooks connector' subcommands."""
    if action == "link":
        _connector_link(Path(path).expanduser().resolve() if path else None)
    elif action == "unlink":
        _connector_unlink(name or "")
    elif action == "list":
        _connector_list()
    elif action == "inspect":
        _connector_inspect(Path(path).expanduser().resolve() if path else None)
    elif action == "new":
        _connector_new(
            conn_name=name,
            conn_path=path,
            description=kwargs.get("description"),
            profiles_list=kwargs.get("profiles"),
            base_env_str=kwargs.get("base_env"),
            auto_link=kwargs.get("auto_link", False),
        )
    else:
        print(f"Unknown connector action: {action}", file=sys.stderr)
        sys.exit(1)


def _connector_link(conn_dir: Path | None) -> None:
    """Link an external connector directory into state.json."""
    if conn_dir is None:
        print("ERROR: Provide the path to a connector directory.", file=sys.stderr)
        sys.exit(1)

    conn_yml = conn_dir / "connector.yml"
    if not conn_yml.exists():
        print(f"ERROR: {conn_dir} does not contain a connector.yml", file=sys.stderr)
        sys.exit(1)

    try:
        meta = yaml.safe_load(conn_yml.read_text())
    except Exception as exc:
        print(f"ERROR: Failed to parse connector.yml: {exc}", file=sys.stderr)
        sys.exit(1)

    conn_name = meta.get("name", conn_dir.name)
    state = _load_state()
    connectors = state.setdefault("connectors", {})

    if conn_name in connectors:
        existing = connectors[conn_name]["path"]
        print(f"Connector '{conn_name}' already linked at {existing}")
        print(f"Updating path to {conn_dir}")

    connectors[conn_name] = {
        "path": str(conn_dir),
        "linked_at": datetime.now(timezone.utc).isoformat(),
    }
    _save_state(state)

    # Show summary
    desc = meta.get("description", "")
    version = meta.get("version", "?")
    profiles_dir = conn_dir / "profiles"
    profile_names = sorted(p.name for p in profiles_dir.iterdir() if p.is_dir()) if profiles_dir.is_dir() else []

    _cprint(f"[OK] Linked connector '{conn_name}' v{version}")
    if desc:
        print(f"     {desc}")
    if profile_names:
        print(f"     Profiles: {', '.join(profile_names)}")
    print()
    print("Run 'agentihooks init' to apply connector rules to your settings.")


def _connector_unlink(conn_name: str) -> None:
    """Unlink a connector by name."""
    if not conn_name:
        print("ERROR: Provide the connector name to unlink.", file=sys.stderr)
        sys.exit(1)

    state = _load_state()
    connectors = state.get("connectors", {})

    if conn_name not in connectors:
        print(f"Connector '{conn_name}' is not linked.", file=sys.stderr)
        available = list(connectors.keys())
        if available:
            print(f"Linked connectors: {', '.join(available)}")
        sys.exit(1)

    del connectors[conn_name]
    _save_state(state)
    _cprint(f"[OK] Unlinked connector '{conn_name}'")
    print()
    print("Run 'agentihooks init' to remove connector rules from your settings.")


def _connector_list() -> None:
    """List all linked connectors."""
    state = _load_state()
    connectors = state.get("connectors", {})

    if not connectors:
        print("No connectors linked.")
        print()
        print("Link one with:")
        print("  agentihooks connector link /path/to/connector")
        return

    print(f"Linked connectors ({len(connectors)}):")
    print()
    for name, info in connectors.items():
        conn_dir = Path(info["path"])
        linked_at = info.get("linked_at", "?")
        exists = conn_dir.is_dir()
        status = "OK" if exists else "MISSING"

        print(f"  {name}")
        print(f"    Path:   {conn_dir}")
        print(f"    Linked: {linked_at}")
        print(f"    Status: {status}")

        if exists:
            conn_yml = conn_dir / "connector.yml"
            if conn_yml.exists():
                try:
                    meta = yaml.safe_load(conn_yml.read_text())
                    desc = meta.get("description", "")
                    version = meta.get("version", "?")
                    if desc:
                        print(f"    Desc:   {desc}")
                    print(f"    Version: {version}")
                except Exception:
                    pass

            profiles_dir = conn_dir / "profiles"
            if profiles_dir.is_dir():
                profile_names = sorted(p.name for p in profiles_dir.iterdir() if p.is_dir())
                if profile_names:
                    print(f"    Profiles: {', '.join(profile_names)}")
        print()


def _connector_inspect(conn_dir: Path | None) -> None:
    """Preview what a connector would merge for each profile."""
    if conn_dir is None:
        print("ERROR: Provide the path to a connector directory.", file=sys.stderr)
        sys.exit(1)

    conn_yml = conn_dir / "connector.yml"
    if not conn_yml.exists():
        print(f"ERROR: {conn_dir} does not contain a connector.yml", file=sys.stderr)
        sys.exit(1)

    try:
        meta = yaml.safe_load(conn_yml.read_text())
    except Exception as exc:
        print(f"ERROR: Failed to parse connector.yml: {exc}", file=sys.stderr)
        sys.exit(1)

    conn_name = meta.get("name", conn_dir.name)
    desc = meta.get("description", "")
    version = meta.get("version", "?")
    base_env = (meta.get("base") or {}).get("env", {})

    print(f"Connector: {conn_name} v{version}")
    if desc:
        print(f"  {desc}")
    print()

    if base_env:
        print("Base env (all profiles):")
        for k, v in base_env.items():
            print(f"  {k}={v}")
        print()

    profiles_dir = conn_dir / "profiles"
    if not profiles_dir.is_dir():
        print("No profile directories found.")
        return

    for profile_dir in sorted(profiles_dir.iterdir()):
        if not profile_dir.is_dir():
            continue
        profile_name = profile_dir.name
        print(f"Profile: {profile_name}")

        perms_file = profile_dir / "permissions.json"
        if perms_file.exists():
            try:
                perms = json.loads(perms_file.read_text())
                deny = perms.get("deny", [])
                if deny:
                    print(f"  Deny rules ({len(deny)}):")
                    for rule in deny:
                        print(f"    - {rule}")
            except Exception as exc:
                print(f"  [ERROR] permissions.json: {exc}")

        env_file = profile_dir / "env.json"
        if env_file.exists():
            try:
                env_data = json.loads(env_file.read_text())
                if env_data:
                    print("  Env overrides:")
                    for k, v in env_data.items():
                        print(f"    {k}={v}")
            except Exception as exc:
                print(f"  [ERROR] env.json: {exc}")

        print()


def _connector_new(
    conn_name: str | None = None,
    conn_path: str | None = None,
    description: str | None = None,
    profiles_list: str | None = None,
    base_env_str: str | None = None,
    auto_link: bool = False,
) -> None:
    """Create a new connector scaffold — interactive or headless.

    Headless mode (all args provided):
        agentihooks connector new --name my-mcp --path ~/dev/tools/connectors --description "..." --profiles default,coding --base-env KEY=VAL,KEY2=VAL2 --link

    Interactive mode (missing args → prompt user):
        agentihooks connector new
    """
    interactive = sys.stdin.isatty()

    # --- Name ---
    if not conn_name:
        if not interactive:
            print("ERROR: --name required in headless mode.", file=sys.stderr)
            sys.exit(1)
        conn_name = input("Connector name (lowercase, hyphens): ").strip()
        if not conn_name:
            print("ERROR: Name cannot be empty.", file=sys.stderr)
            sys.exit(1)

    # --- Path ---
    if not conn_path:
        if not interactive:
            print("ERROR: --path required in headless mode.", file=sys.stderr)
            sys.exit(1)
        default_path = str(Path.cwd() / "connectors")
        conn_path = input(f"Parent directory [{default_path}]: ").strip() or default_path

    parent = Path(conn_path).expanduser().resolve()
    conn_dir = parent / conn_name

    if conn_dir.exists():
        print(f"ERROR: {conn_dir} already exists.", file=sys.stderr)
        sys.exit(1)

    # --- Description ---
    if not description:
        if interactive:
            description = input("Description (optional): ").strip()
        description = description or f"Connector: {conn_name}"

    # --- Profiles ---
    available = _available_profiles()
    if not profiles_list:
        if interactive:
            print(f"Available profiles: {', '.join(available)}")
            profiles_list = input(f"Profiles to create (comma-separated) [{','.join(available)}]: ").strip()
        if not profiles_list:
            profiles_list = ",".join(available)

    profiles = [p.strip() for p in profiles_list.split(",") if p.strip()]

    # --- Base env ---
    base_env: dict[str, str] = {}
    if base_env_str:
        for pair in base_env_str.split(","):
            if "=" in pair:
                k, v = pair.split("=", 1)
                base_env[k.strip()] = v.strip()
    elif interactive:
        print("Base env vars (applied to all profiles). Empty line to finish.")
        while True:
            pair = input("  KEY=VALUE: ").strip()
            if not pair:
                break
            if "=" in pair:
                k, v = pair.split("=", 1)
                base_env[k.strip()] = v.strip()
            else:
                print("  Invalid format — use KEY=VALUE")

    # --- Create structure ---
    conn_dir.mkdir(parents=True)

    # connector.yml
    yml_data = {"name": conn_name, "description": description, "version": "1.0.0"}
    if base_env:
        yml_data["base"] = {"env": base_env}

    (conn_dir / "connector.yml").write_text(yaml.dump(yml_data, default_flow_style=False, sort_keys=False))

    # profiles
    for profile_name in profiles:
        profile_dir = conn_dir / "profiles" / profile_name
        profile_dir.mkdir(parents=True)
        (profile_dir / "permissions.json").write_text(json.dumps({"deny": []}, indent=2) + "\n")

    _cprint(f"[OK] Created connector at {conn_dir}")
    print()
    print("Structure:")
    print(f"  {conn_dir}/")
    print("    connector.yml")
    for profile_name in profiles:
        print(f"    profiles/{profile_name}/permissions.json")
    print()
    print("Next steps:")
    print("  1. Edit profiles/*/permissions.json with deny rules")
    print(f"  2. agentihooks connector link {conn_dir}")
    print("  3. agentihooks init --profile <name>")

    # --- Auto-link ---
    if auto_link:
        _connector_link(conn_dir)
    elif interactive:
        answer = input("\nLink this connector now? [y/N] ").strip().lower()
        if answer == "y":
            _connector_link(conn_dir)


# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Per-repo config (agentihooks init)
# ---------------------------------------------------------------------------


def _cmd_settings_profile(args: argparse.Namespace) -> None:
    """Quick-switch: re-apply only the settings layer without touching rules/CLAUDE.md."""
    state = _load_state()
    global_target = state.get("targets", {}).get("global", {})
    current_profile = global_target.get("profile", "")

    if not current_profile:
        print("ERROR: No profile installed. Run 'agentihooks init' first.", file=sys.stderr)
        sys.exit(1)

    if getattr(args, "clear", False):
        # Remove settings overlay — re-init with persona profile only
        print(f"Clearing settings overlay, reverting to profile '{current_profile}' defaults...")
        global_args = argparse.Namespace(profile=current_profile, settings_profile="")
        install_global(global_args)
        return

    sp_name = getattr(args, "sp_name", None)
    if not sp_name:
        # Show current state
        current_sp = global_target.get("settings_profile", "")
        print(f"Persona profile  : {current_profile}")
        print(f"Settings profile : {current_sp or '(none — using persona defaults)'}")
        print()
        available = _available_profiles()
        print(f"Available: {', '.join(available)}")
        print()
        print("Usage:")
        print("  agentihooks settings-profile <name>    # switch settings layer")
        print("  agentihooks settings-profile --clear   # revert to persona defaults")
        return

    # Apply the new settings profile on top of the existing persona profile
    print(f"Switching settings layer to '{sp_name}' (keeping persona '{current_profile}')...")
    global_args = argparse.Namespace(profile=current_profile, settings_profile=sp_name)
    install_global(global_args)


def _clean_state_dir() -> None:
    """Reset install state + session caches for a fresh install. Preserves persistent data."""
    import shutil

    state_dir = AGENTIHOOKS_STATE_DIR
    if not state_dir.exists():
        return

    # Whitelist: only these are deleted. Everything else survives.
    _DELETE_FILES = {
        "state.json",
        "sync-hashes.json",
        "sync.lock",
        "sync-daemon.pid",
        "sync-daemon.heartbeat",
        ".sync-daemon.singleton.lock",
        "active-sessions.json",
        "mcp-tool-cache.json",
        "broadcast_delivery_state.json",
    }
    _DELETE_DIRS = {
        "controls_flags",
        "voice_flags",
        "prod_bypass",
        "force_refresh",
        "state",
    }
    _DELETE_GLOBS = ["ctx_refresh_*.json", "*.pid"]

    removed = 0
    for name in _DELETE_FILES:
        target = state_dir / name
        if target.exists():
            try:
                target.unlink()
                removed += 1
            except Exception as e:
                print(f"  {_YELLOW}[WARN] Could not remove {name}: {e}{_RESET}")
    for name in _DELETE_DIRS:
        target = state_dir / name
        if target.is_dir():
            try:
                shutil.rmtree(target)
                removed += 1
            except Exception as e:
                print(f"  {_YELLOW}[WARN] Could not remove {name}/: {e}{_RESET}")
    for pattern in _DELETE_GLOBS:
        for target in state_dir.glob(pattern):
            try:
                target.unlink()
                removed += 1
            except Exception:
                pass

    # Clean ~/.claude/ symlinked assets and generated files
    claude_dir = CLAUDE_HOME
    for subdir in ("rules", "skills", "agents", "commands"):
        target = claude_dir / subdir
        if target.is_symlink() or target.is_dir():
            try:
                if target.is_symlink():
                    target.unlink()
                else:
                    shutil.rmtree(target)
                removed += 1
            except Exception:
                pass
    for fname in ("settings.json", "settings.local.json", "CLAUDE.md"):
        target = claude_dir / fname
        if target.exists() or target.is_symlink():
            try:
                target.unlink()
                removed += 1
            except Exception:
                pass

    print(f"  {_GREEN}[OK] Clean install: reset {removed} items (install state + session caches){_RESET}")
    print(f"  {_DIM}[--] Preserved: broadcasts, enforcements, brain data, logs, .env, .venv{_RESET}")


def cmd_init_unified(args: argparse.Namespace) -> None:
    """Unified init command — global install only.

    agentihooks init --bundle <path>    → link bundle + global install
    agentihooks init                    → re-run global install (bundle must be linked)
    agentihooks init --force            → clean install (wipe state, re-init from scratch)
    """
    if getattr(args, "dry_run", False):
        # The flag was accepted by argparse and read nowhere, so `init --dry-run`
        # performed a full destructive install — rewriting CLAUDE.md, MCP config,
        # ~/.bashrc and reinstalling the CLI — while its help promised a preview.
        # Refusing loudly beats silently doing the opposite of what was asked.
        print(
            "ERROR: `init --dry-run` is not implemented — init writes CLAUDE.md, MCP\n"
            "config, ~/.bashrc and the CLI, and none of that is previewable today.\n"
            "Refusing rather than performing a real install under a --dry-run flag.\n"
            "\n"
            "To inspect without installing:\n"
            "  agentihooks status          # current install state\n"
            "  agentihooks doctor          # hook health\n"
            "  agentihooks --list-profiles # resolvable profiles",
            file=sys.stderr,
        )
        sys.exit(2)

    if getattr(args, "force", False):
        _clean_state_dir()
    bundle_path = getattr(args, "bundle", None)

    # Global mode
    if bundle_path:
        # First-time: link bundle, then global install
        bundle_dir = Path(bundle_path).expanduser().resolve()
        if not bundle_dir.is_dir():
            print(f"ERROR: Bundle directory not found: {bundle_dir}", file=sys.stderr)
            sys.exit(1)
        # Check for profiles/ dir in bundle (new structure) or .agentihooks/ (legacy)
        if not (bundle_dir / "profiles").is_dir():
            # Maybe they pointed at the old .agentihooks subdir?
            if (bundle_dir.parent / "profiles").is_dir():
                print("HINT: Point --bundle at the repo root, not .agentihooks/")
            print(f"ERROR: No profiles/ directory found in {bundle_dir}", file=sys.stderr)
            sys.exit(1)
        _bundle_link(bundle_dir)
        print()

    # B1: process --link-profile NAME=PATH (repeatable) BEFORE profile resolution
    # so the new profile names are visible to the chain validation downstream.
    link_profile_args = list(getattr(args, "link_profile", []) or [])
    just_linked_names: list[str] = []
    for spec in link_profile_args:
        if "=" not in spec:
            print(f"ERROR: --link-profile expects NAME=PATH, got {spec!r}", file=sys.stderr)
            sys.exit(1)
        name, _, lp_path = spec.partition("=")
        name = name.strip()
        lp_path = lp_path.strip()
        if not name or not lp_path:
            print(f"ERROR: --link-profile expects NAME=PATH, got {spec!r}", file=sys.stderr)
            sys.exit(1)
        lp_dir = Path(lp_path).expanduser().resolve()
        if not lp_dir.is_dir():
            print(f"ERROR: --link-profile path is not a directory: {lp_dir}", file=sys.stderr)
            sys.exit(1)
        # run_init=False — we're about to install_global below; don't trigger an
        # extra round-trip from inside link-profile.
        _link_profile_link(lp_dir, name=name, append=True, run_init=False)
        just_linked_names.append(name)

    # Check bundle (optional — works without one using built-in profiles only)
    bundle = _get_bundle_path()
    if not bundle:
        print(f"{_DIM}[--] No bundle linked — using built-in profiles only.{_RESET}")
        # B2: auto-discover hint — don't auto-link, just suggest the command.
        if not getattr(args, "no_discover", False):
            _hint_state = _load_state()
            _hint_profile = (
                getattr(args, "init_profile", None)
                or _hint_state.get("targets", {}).get("global", {}).get("profile")
                or "default"
            )
            _print_bundle_discover_hint(profile_hint=_hint_profile)

    # Resolve profile — prefer: CLI flag > env var > state.json > interactive prompt
    _is_force = getattr(args, "force", False)
    profile_name = args.profile
    if not profile_name:
        profile_name = os.environ.get("AGENTIHOOKS_PROFILE", "")
    _prev_state = _load_state()
    _migrate_profile_rename(_prev_state, "colt", "anton")
    _prev_global = _prev_state.get("targets", {}).get("global", {})
    if not profile_name:
        if _is_force:
            # --force = fresh install, ignore stored profile
            profile_name = "default"
        elif _prev_global.get("profile", ""):
            profile_name = _prev_global["profile"]
            print(f"Using profile from previous install: {profile_name}")
        elif sys.stdin.isatty():
            available = _available_profiles()
            print(f"Available profiles: {', '.join(available)}")
            profile_name = input("Profile [default]: ").strip() or "default"
        else:
            profile_name = "default"

    # B1.1: profiles freshly linked in THIS invocation must end up in the
    # install chain even when the operator also passed --profile <name>.
    # Without this merge, --profile clobbers the chain that --link-profile
    # just appended in state.json, and the linked profile is registered but
    # not installed.
    if just_linked_names:
        existing_chain = [p.strip() for p in profile_name.split(",") if p.strip()]
        for lname in just_linked_names:
            if lname not in existing_chain:
                existing_chain.append(lname)
        profile_name = ",".join(existing_chain)

    # Guard: a bare `init` that resolved to 'default' while the managed
    # CLAUDE.md on disk was installed from a different profile means state.json
    # has lost (or never had) the profile record — silently stomping the live
    # profile with 'default' is exactly the failure this blocks. Explicit
    # --profile / AGENTIHOOKS_PROFILE / --force still win.
    if (
        profile_name == "default"
        and not args.profile
        and not os.environ.get("AGENTIHOOKS_PROFILE", "")
        and not _is_force
    ):
        _md_path = Path(_prev_state.get("managed_claude_md") or (Path.home() / ".claude" / "CLAUDE.md"))
        _marker_profile = ""
        try:
            _text = _md_path.read_text() if _md_path.exists() else ""
            # Scan the whole file, not just line 1 — a chained install writes
            # one `<!-- profile: X -->` marker per chain member, and the
            # re-run suggestion below must carry the full chain, not just the
            # first entry. findall on "" (empty CLAUDE.md) returns [] rather
            # than raising, so an existing-but-empty file is handled too.
            _matches = re.findall(r"<!--\s*profile:\s*([A-Za-z0-9_,-]+)\s*-->", _text)
            _seen: set[str] = set()
            _chain: list[str] = [m for m in _matches if not (m in _seen or _seen.add(m))]
            _marker_profile = ",".join(_chain)
        except OSError:
            pass
        if _marker_profile and _marker_profile != "default":
            print(
                f"ERROR: state.json has no stored profile, but the installed CLAUDE.md "
                f"was built from profile '{_marker_profile}'.\n"
                f"Refusing to overwrite it with 'default'. Re-run with the intended profile:\n"
                f"  agentihooks init --profile {_marker_profile}\n"
                f"or force a clean default install with: agentihooks init --force",
                file=sys.stderr,
            )
            sys.exit(1)

    # Resolve settings profile (optional overlay) — same precedence as profile
    settings_profile = getattr(args, "settings_profile", None)
    if not settings_profile:
        settings_profile = os.environ.get("AGENTIHOOKS_SETTINGS_PROFILE", "")
    if not settings_profile and not _is_force:
        settings_profile = _prev_global.get("settings_profile", "")

    # Build args for _install_global_inner
    global_args = argparse.Namespace(profile=profile_name, settings_profile=settings_profile or "")
    install_global(global_args)

    # --- Update bashrc block (agentienv + agenti alias + PATH) ---
    # Always write — the block is self-guarding (agentienv checks for the
    # env file at runtime) and the alias/PATH lines must land regardless.
    _update_bashrc_block()


def _remove_bashrc_block() -> bool:
    """Remove the managed agentihooks block from ~/.bashrc. Returns True if found."""
    if not _BASHRC.exists():
        return False
    text = _BASHRC.read_text(encoding="utf-8")
    if _BLOCK_START not in text:
        return False
    lines = text.splitlines(keepends=True)
    new_lines = []
    inside = False
    for line in lines:
        if line.rstrip() == _BLOCK_START:
            inside = True
        elif line.rstrip() == _BLOCK_END:
            inside = False
        elif not inside:
            new_lines.append(line)
    _BASHRC.write_text("".join(new_lines), encoding="utf-8")
    return True


def _update_bashrc_block() -> None:
    """Remove old block then append fresh one at the end of ~/.bashrc."""
    env_file = _ENV_FILE_DST
    env_dir = env_file.parent
    block = (
        f"{_BLOCK_START}\n"
        f"agentienv() {{\n"
        f"  local _c=0\n"
        f"  set -a\n"
        f'  if [[ -f "{env_file}" ]]; then\n'
        f'    . "{env_file}" 2>/dev/null && _c=$((_c + 1))\n'
        f'    for f in "{env_dir}"/*.env; do\n'
        f'      [[ -f "$f" ]] && [[ "$f" != "{env_file}" ]] && {{ . "$f" 2>/dev/null && _c=$((_c + 1)); }}\n'
        f"    done\n"
        f"  fi\n"
        f'  if [[ -f "$HOME/.env" ]]; then\n'
        f'    . "$HOME/.env" 2>/dev/null && _c=$((_c + 1))\n'
        f"  fi\n"
        f"  set +a\n"
        f"  if (( _c > 0 )); then\n"
        f'    echo "[agentienv] loaded $_c env file(s)"\n'
        f"  fi\n"
        f"}}\n"
        f"agentienv\n"
        f'case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) export PATH="$HOME/.local/bin:$PATH" ;; esac\n'
        f'alias agenti="agentihooks claude"\n'
        f"{_BLOCK_END}\n"
    )

    # Always remove first, then append at the end
    _remove_bashrc_block()
    bashrc_text = _BASHRC.read_text(encoding="utf-8") if _BASHRC.exists() else ""
    sep = "\n" if bashrc_text and not bashrc_text.endswith("\n") else ""
    _BASHRC.write_text(bashrc_text + sep + block, encoding="utf-8")
    _cprint(f"{_YELLOW}[OK] agentihooks block written to {_BASHRC}{_RESET}")
    print(f"{_DIM}     run: source ~/.bashrc{_RESET}")


# User env file (~/.agentihooks/.env)
# ---------------------------------------------------------------------------

_ENV_FILE_DST = AGENTIHOOKS_STATE_DIR / ".env"
_ENV_EXAMPLE_SRC = AGENTIHOOKS_ROOT / ".env.example"


def _seed_user_env_file() -> None:
    """Create ~/.agentihooks/.env from .env.example if it doesn't already exist.

    Never overwrites — only creates on first install.
    The user is the only one who should delete or modify this file.
    """
    AGENTIHOOKS_STATE_DIR.mkdir(parents=True, exist_ok=True)
    if _ENV_FILE_DST.exists():
        _cprint(f"  [--] {_ENV_FILE_DST} already exists — not overwritten (your file)")
        return
    if _ENV_EXAMPLE_SRC.exists():
        shutil.copy2(_ENV_EXAMPLE_SRC, _ENV_FILE_DST)
        _cprint(f"  [OK] Created {_ENV_FILE_DST}")
        print(f"       Configure your integrations: {_ENV_FILE_DST}")
    else:
        # Wheel install — .env.example isn't packaged. Create an empty
        # placeholder so the bashrc block has a valid source target.
        _ENV_FILE_DST.write_text(
            "# agentihooks user env file — add KEY=VALUE lines here\n",
            encoding="utf-8",
        )
        _cprint(f"  [OK] Created empty {_ENV_FILE_DST} (no .env.example in wheel)")


# ---------------------------------------------------------------------------
# --loadenv: install agentihooksenv alias into ~/.bashrc (managed block)
# ---------------------------------------------------------------------------

_BASHRC = Path.home() / ".bashrc"
_BLOCK_START = "# === agentihooks ==="
_BLOCK_END = "# === end-agentihooks ==="


def _cmd_loadenv(env_file: Path, exec_cmd: list[str], *, force: bool = False) -> None:
    """Write a managed alias block into ~/.bashrc so `agentienv` sources the .env."""
    if not env_file.is_file():
        _cprint(f"[!!] env file not found: {env_file}", file=sys.stderr)
        sys.exit(1)

    env_dir = env_file.parent
    block = (
        f"{_BLOCK_START}\n"
        f"agentienv() {{\n"
        f'  if [ ! -f "{env_file}" ]; then\n'
        f'    echo "[agentienv] no .env found at {env_file} — skipping"\n'
        f"    return 0\n"
        f"  fi\n"
        f"  set -a\n"
        f'  . "{env_file}" 2>/dev/null || {{ echo "[agentienv] ERROR: failed to source {env_file}"; set +a; return 1; }}\n'
        f"  _aih_count=1\n"
        f'  for f in "{env_dir}"/*.env; do\n'
        f'    [ -f "$f" ] && [ "$f" != "{env_file}" ] && {{\n'
        f'      . "$f" 2>/dev/null || echo "[agentienv] WARNING: failed to source $f"\n'
        f"      _aih_count=$((_aih_count + 1))\n"
        f"    }}\n"
        f"  done\n"
        f"  set +a\n"
        f'  echo "[agentienv] loaded $_aih_count env file(s) from {env_dir}"\n'
        f"}}\n"
        f"agentienv\n"
        f"# alias: launch claude with profile flags + env\n"
        f'alias agenti="agentihooks claude"\n'
        f"{_BLOCK_END}\n"
    )

    bashrc_text = _BASHRC.read_text(encoding="utf-8") if _BASHRC.exists() else ""

    if _BLOCK_START in bashrc_text:
        # Replace existing block
        lines = bashrc_text.splitlines(keepends=True)
        new_lines = []
        inside = False
        for line in lines:
            if line.rstrip() == _BLOCK_START:
                inside = True
                new_lines.append(block)
            elif line.rstrip() == _BLOCK_END:
                inside = False
            elif not inside:
                new_lines.append(line)
        _BASHRC.write_text("".join(new_lines), encoding="utf-8")
        _cprint(f"[OK] Updated agentihooks block in {_BASHRC}")
    else:
        # Append new block
        sep = "\n" if bashrc_text and not bashrc_text.endswith("\n") else ""
        _BASHRC.write_text(bashrc_text + sep + block, encoding="utf-8")
        _cprint(f"[OK] Added agentihooks block to {_BASHRC}")

    print()
    print("Now reload your shell and use:")
    print("  source ~/.bashrc")
    print("  agentienv")
    print()

    _prompt_install_requirements(force=force)


def _find_requirements_files() -> list[Path]:
    """Return requirements.txt files found in ~/.agentihooks/ and mcpLibPath."""
    candidates: list[Path] = []
    for search_dir in [AGENTIHOOKS_STATE_DIR, _state_get_mcp_lib()]:
        if search_dir is None:
            continue
        req = search_dir / "requirements.txt"
        if req.is_file() and req not in candidates:
            candidates.append(req)
    return candidates


def _detect_venv() -> Path | None:
    """Return the Python executable inside the active or local venv, or None.

    Priority — operator intent FIRST. The activated VIRTUAL_ENV wins over
    any directory-based discovery, so a stray ``.venv`` in the cwd (created
    by tools like ``uv run``) cannot silently shadow an explicitly
    activated environment.

    1. ``$VIRTUAL_ENV`` — explicit operator activation. Highest authority.
    2. Dedicated ``~/.agentihooks/.venv`` — canonical fallback if the
       operator has set one up but isn't currently activated.
    3. ``./.venv`` in cwd — last-resort for plain ``cd repo && python``
       workflows. Anything created here by ``uv run`` or similar tools
       loses to (1) and (2).
    """
    # 1. Activated venv via VIRTUAL_ENV — explicit operator intent wins
    venv_env = os.environ.get("VIRTUAL_ENV")
    if venv_env:
        python = Path(venv_env) / "bin" / "python"
        if python.exists():
            return python

    # 2. Dedicated ~/.agentihooks/.venv (canonical opt-in)
    agentihooks_venv = Path.home() / ".agentihooks" / ".venv" / "bin" / "python"
    if agentihooks_venv.exists():
        return agentihooks_venv

    # 3. .venv directory in cwd — lowest priority
    local_venv = Path.cwd() / ".venv" / "bin" / "python"
    if local_venv.exists():
        return local_venv

    return None


def _python_can_import_hooks(python_path: Path | str) -> bool:
    """Return True iff *python_path* can ``import hooks`` without cwd help.

    The MCP server is launched by Claude Code from its own cwd; relying on
    cwd-on-sys.path to make ``import hooks`` work is fragile and has caused
    silent ``ModuleNotFoundError`` failures. This probe runs the candidate
    python from a neutral directory so cwd cannot rescue a venv that is
    missing the editable install.
    """
    import subprocess

    try:
        result = subprocess.run(
            [str(python_path), "-c", "import hooks"],
            cwd="/",
            capture_output=True,
            timeout=5,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _prompt_install_requirements(*, force: bool = False) -> None:
    """Discover requirements.txt files and offer to install each with uv.

    *force* skips venv detection and installs into system Python (for Docker/CI).
    """
    import subprocess

    req_files = _find_requirements_files()
    if not req_files:
        return

    uv = shutil.which("uv")
    if not uv:
        _cprint("  [!!] uv not found — skipping requirements install.")
        return

    for req in req_files:
        try:
            answer = input(f"Found {req} — install with uv? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\nSkipped.")
            return

        if answer != "y":
            _cprint("  [--] Skipped.")
            continue

        if force:
            python = Path(sys.executable)
            print(f"  [..] Installing into system Python ({python}) ...")
        else:
            python = _detect_venv()
            if python is None:
                _cprint("  [!!] No virtual environment found.")
                print("       Create and activate one first:")
                print("         python3 -m venv .venv && source .venv/bin/activate")
                print("       Or use --force to install into system Python (Docker/CI).")
                print("       Then re-run: agentihooks --loadenv")
                continue
            print(f"  [..] Installing into {python.parent.parent} ...")
        result = subprocess.run(
            [uv, "pip", "install", "--python", str(python), "-r", str(req)],
            capture_output=False,
        )
        if result.returncode == 0:
            _cprint(f"  [OK] Installed {req}")
        else:
            _cprint(f"  [!!] uv pip install failed (exit {result.returncode})")


# ---------------------------------------------------------------------------
# Path substitution: replace /app with the actual agentihooks root
# ---------------------------------------------------------------------------


def substitute_paths(obj: object, src: str = "/app", dst: str = str(AGENTIHOOKS_ROOT)) -> object:
    """Recursively replace *src* with *dst* in all string values of a JSON structure."""
    if isinstance(obj, str):
        return obj.replace(src, dst)
    if isinstance(obj, dict):
        return {k: substitute_paths(v, src, dst) for k, v in obj.items()}
    if isinstance(obj, list):
        return [substitute_paths(item, src, dst) for item in obj]
    return obj


def _resolve_profile_hook_paths(settings: dict, profile_dir: Path) -> dict:
    """Resolve relative paths in hook commands against the profile's root directory.

    Profile settings.overrides.json may contain hook commands with relative paths
    like ``bash profiles/patch-mode/hooks/foo.sh``. These need to be absolute so
    they work regardless of the user's CWD when Claude runs.
    """
    hooks = settings.get("hooks")
    if not hooks or not isinstance(hooks, dict):
        return settings

    settings = deepcopy(settings)
    for _event, matchers in settings["hooks"].items():
        if not isinstance(matchers, list):
            continue
        for matcher in matchers:
            for hook in matcher.get("hooks", []):
                cmd = hook.get("command")
                if not cmd or not isinstance(cmd, str):
                    continue
                # Split to find the script path (e.g. "bash profiles/foo/bar.sh")
                parts = cmd.split()
                for i, part in enumerate(parts):
                    # Skip flags and the interpreter itself
                    if part.startswith("-") or i == 0:
                        continue
                    # Check relative to profile dir, bundle root (profiles/..), and agentihooks root
                    for base in (profile_dir, profile_dir.parent.parent, AGENTIHOOKS_ROOT):
                        candidate = base / part
                        if candidate.exists():
                            parts[i] = str(candidate.resolve())
                            break
                    else:
                        continue
                    break
                hook["command"] = " ".join(parts)
    return settings


def _available_profiles() -> list[str]:
    """Return profile names from built-in profiles/, linked bundle, and linked external dirs."""
    names = {d.name for d in PROFILES_DIR.iterdir() if d.is_dir() and not d.name.startswith("_")}
    bundle = _get_bundle_path()
    if bundle:
        bp = bundle / "profiles"
        if bp.is_dir():
            names.update(d.name for d in bp.iterdir() if d.is_dir() and not d.name.startswith("_"))
    for entry in _get_linked_profiles():
        name = entry.get("name", "")
        path = Path(entry.get("path", ""))
        if name and path.is_dir():
            names.add(name)
    return sorted(names)


def query_active_profile() -> None:
    """Print the active global profile."""
    source = "global"
    state = _load_state()
    global_target = state.get("targets", {}).get("global", {})
    profile_name = global_target.get("profile")
    settings_profile = global_target.get("settings_profile", "")

    if not profile_name:
        print("not installed")
        return

    chain = [p.strip() for p in profile_name.split(",") if p.strip()]
    if len(chain) > 1:
        print(f"chain: [{', '.join(chain)}] ({source})")
    else:
        print(f"{profile_name} ({source})")
    if settings_profile:
        print(f"settings: {settings_profile}")


def list_profiles() -> None:
    """Print all available profiles (built-in + bundle) and exit."""
    profiles = _available_profiles()
    if not profiles:
        print("No profiles found.")
        return

    print("Available profiles:\n")
    for name in profiles:
        profile_dir = _resolve_profile_dir(name)
        if not profile_dir:
            continue
        source = _profile_source_label(name)
        marker = ""
        claude_md = profile_dir / _CLAUDE_MD_NAME
        if not claude_md.exists():
            marker = f"  [no {_CLAUDE_MD_NAME}]"
        print(f"  {name} ({source}){marker}")
    print()


# ---------------------------------------------------------------------------
# Settings helpers
# ---------------------------------------------------------------------------


def _preserve_personal_keys(existing_path: Path) -> dict:
    """Return personal keys from an existing unmanaged settings file."""
    personal: dict = {}
    if not existing_path.exists():
        return personal
    try:
        existing = load_json(existing_path)
        if existing.get(MANAGED_BY_KEY) == MANAGED_BY_VALUE:
            return personal  # Already managed — don't re-import
        for key in PERSONAL_KEYS:
            if key in existing:
                personal[key] = existing[key]
        if personal:
            print(f"Preserving personal keys from existing settings: {sorted(personal)}")
    except json.JSONDecodeError:
        print("WARNING: existing settings.json is invalid JSON – skipping preservation.")
    return personal


def _backup_settings(existing_path: Path) -> None:
    """Back up an existing unmanaged settings file (skips if already managed)."""
    if not existing_path.exists():
        return
    existing_raw = load_json(existing_path)
    if existing_raw.get(MANAGED_BY_KEY) == MANAGED_BY_VALUE:
        return
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = existing_path.with_suffix(f".json.bak.{timestamp}")
    shutil.copy2(existing_path, backup_path)
    print(f"Backed up existing settings → {backup_path}")


# ---------------------------------------------------------------------------
# OTEL env vars builder
# ---------------------------------------------------------------------------


def _build_otel_env(profile_data: dict) -> dict:
    """Build OTEL env vars from profile otel config.

    Reads the ``otel:`` section from a profile.yml and returns a dict
    of env vars to inject into settings.json ``env`` block.
    """
    otel_cfg = profile_data.get("otel", {})
    if not otel_cfg.get("enabled", True):
        return {}

    env: dict[str, str] = {
        "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
        "OTEL_METRICS_EXPORTER": "otlp",
        "OTEL_LOGS_EXPORTER": "otlp",
        "OTEL_EXPORTER_OTLP_PROTOCOL": otel_cfg.get("protocol", "grpc"),
        "OTEL_LOG_TOOL_DETAILS": "1",
    }

    endpoint = otel_cfg.get("endpoint")
    if endpoint:
        env["OTEL_EXPORTER_OTLP_ENDPOINT"] = endpoint

    attrs = otel_cfg.get("resource_attributes", {})
    if attrs:
        env["OTEL_RESOURCE_ATTRIBUTES"] = ",".join(f"{k}={v}" for k, v in attrs.items())

    # Langfuse destination (traces only, OTLP HTTP)
    langfuse = otel_cfg.get("langfuse", {})
    if langfuse.get("enabled"):
        env["OTEL_LANGFUSE_ENABLED"] = "1"
        if langfuse.get("endpoint"):
            env["OTEL_LANGFUSE_ENDPOINT"] = langfuse["endpoint"]
        if langfuse.get("public_key"):
            env["OTEL_LANGFUSE_PUBLIC_KEY"] = langfuse["public_key"]
        if langfuse.get("secret_key"):
            env["OTEL_LANGFUSE_SECRET_KEY"] = langfuse["secret_key"]

    return env


# ---------------------------------------------------------------------------
# Global install
# ---------------------------------------------------------------------------


def install_global(args: argparse.Namespace) -> None:
    with _sync_lock():
        _install_global_inner(args)


def _install_global_inner(args: argparse.Namespace) -> None:
    profile_input: str = args.profile

    # --- Parse profile chain (comma-separated) ---
    profile_chain = [p.strip() for p in profile_input.split(",") if p.strip()]
    if not profile_chain:
        print("ERROR: No profile specified.", file=sys.stderr)
        sys.exit(1)

    # Validate profiles — drop unresolvable entries with a hint, only exit if all fail
    profile_dirs: list[tuple[str, Path]] = []
    linked_names = {e.get("name") for e in _get_linked_profiles()}
    surviving_chain: list[str] = []
    for pname in profile_chain:
        pdir = _resolve_profile_dir(pname)
        if pdir is None:
            if pname in linked_names:
                _cprint(
                    f"  [WARN] Linked profile '{pname}' path is missing — "
                    f"run 'agentihooks link-profile unlink {pname}' to clean up. Dropping from chain."
                )
            else:
                _cprint(f"  [WARN] Profile '{pname}' not found — dropping from chain.")
            continue
        profile_dirs.append((pname, pdir))
        surviving_chain.append(pname)

    if not profile_dirs:
        available = _available_profiles()
        print(f"ERROR: No profile in chain '{profile_input}' could be resolved.", file=sys.stderr)
        print(f"Available profiles: {', '.join(available)}", file=sys.stderr)
        sys.exit(1)

    profile_chain = surviving_chain
    # In-memory chain string — used for display only. STATE persistence below
    # uses ``profile_input`` (operator intent) so a transient missing profile
    # source (e.g. a git checkout in the bundle repo briefly removing files)
    # cannot shrink the persisted chain.
    profile_name = ",".join(profile_chain)
    persisted_profile = profile_input.strip() or profile_name

    profile_sources = []
    for pname, _ in profile_dirs:
        src = _profile_source_label(pname)
        profile_sources.append(f"{pname} ({src})")

    print(f"{_BOLD}agentihooks root{_RESET} : {AGENTIHOOKS_ROOT}")
    print(f"Target           : {CLAUDE_HOME}")
    if len(profile_chain) > 1:
        print(f"Profile chain    : {' → '.join(profile_sources)}")
    else:
        print(f"Profile source   : {profile_sources[0]}")
        print(f"Profile          : {profile_name}")
    _settings_profile_display = getattr(args, "settings_profile", "") or ""
    if _settings_profile_display:
        sp_src = _profile_source_label(_settings_profile_display)
        print(f"Settings profile : {_settings_profile_display} ({sp_src})")
    _canonical_python = str(_detect_venv() or sys.executable)
    print(f"Python           : {_canonical_python}")
    print()

    # --- 1. Load and render base settings ---
    if not BASE_SETTINGS.exists():
        print(f"ERROR: {BASE_SETTINGS} not found.", file=sys.stderr)
        sys.exit(1)

    raw_settings = load_json(BASE_SETTINGS)
    rendered: dict = substitute_paths(raw_settings)
    rendered = substitute_paths(rendered, "__PYTHON__", _canonical_python)

    # --- 1b. Apply per-profile overrides (chained: each profile merges on top) ---
    for pname, pdir in profile_dirs:
        overrides_path = pdir / _CLAUDE_SUBDIR / "settings.overrides.json"
        if not overrides_path.exists():
            overrides_path = pdir / "settings.overrides.json"
        if overrides_path.exists():
            overrides = load_json(overrides_path)
            # Resolve relative paths in hook commands against the profile's root
            overrides = _resolve_profile_hook_paths(overrides, pdir)
            rendered = _deep_merge(rendered, overrides)
            print(f"Applied profile overrides: {overrides_path}")

    # --- 1b2. Apply settings-profile overlay (settings.json + MCP only, not rules/CLAUDE.md) ---
    settings_profile_name: str = getattr(args, "settings_profile", "") or ""
    settings_profile_dir: Path | None = None
    if settings_profile_name:
        settings_profile_dir = _resolve_profile_dir(settings_profile_name)
        if settings_profile_dir is None:
            available = _available_profiles()
            print(f"ERROR: Settings profile '{settings_profile_name}' not found.", file=sys.stderr)
            print(f"Available profiles: {', '.join(available)}", file=sys.stderr)
            sys.exit(1)

        sp_overrides = settings_profile_dir / _CLAUDE_SUBDIR / "settings.overrides.json"
        if not sp_overrides.exists():
            sp_overrides = settings_profile_dir / "settings.overrides.json"
        if sp_overrides.exists():
            sp_data = load_json(sp_overrides)
            sp_data = _resolve_profile_hook_paths(sp_data, settings_profile_dir)
            rendered = _deep_merge(rendered, sp_data)
            src = _profile_source_label(settings_profile_name)
            print(f"Applied settings-profile overlay: {settings_profile_name} ({src})")

    # --- 1c. Apply linked connectors (from last profile in chain) ---
    last_profile = profile_chain[-1]
    conn_env, conn_deny, conn_disabled = _load_connectors(last_profile)
    if conn_env:
        rendered.setdefault("env", {}).update(conn_env)
        _cprint(f"  [OK] Connector env: {list(conn_env.keys())}")
    if conn_deny:
        rendered.setdefault("permissions", {}).setdefault("deny", []).extend(conn_deny)
        _cprint(f"  [OK] Connector deny rules: {len(conn_deny)}")
    if conn_disabled:
        existing_disabled = rendered.get("disabledMcpjsonServers", [])
        merged_disabled = list(dict.fromkeys(existing_disabled + conn_disabled))
        rendered["disabledMcpjsonServers"] = merged_disabled
        _cprint(f"  [OK] Connector disabled MCP servers: {merged_disabled}")

    # NOTE: OTEL env injection used to read otel: from profile.yml here.
    # That dependency was removed 2026-05-07. _build_otel_env helper is
    # retained for re-wiring through a different mechanism later.

    # --- 2. Merge personal keys from existing settings ---
    existing_settings_path = CLAUDE_HOME / "settings.json"
    personal = _preserve_personal_keys(existing_settings_path)
    merged: dict = deepcopy(personal)
    merged.update(rendered)
    merged[MANAGED_BY_KEY] = MANAGED_BY_VALUE

    # --- 3. Backup + write ---
    _backup_settings(existing_settings_path)
    CLAUDE_HOME.mkdir(parents=True, exist_ok=True)
    save_json(existing_settings_path, merged)
    _cprint(f"[OK] Wrote {existing_settings_path}")

    # --- 4. Symlink skills/agents/commands/rules (layers: agentihooks → bundle → each profile in chain) ---
    bundle_dir = _get_bundle_path()

    for subdir, label, filter_fn in [
        ("skills", "skill", lambda p: p.is_dir()),
        ("agents", "agent", lambda p: p.suffix == ".md" and p.name != "README.md"),
        ("commands", "command", lambda p: p.suffix == ".md" and p.name != "README.md"),
        ("rules", "rule", lambda p: p.suffix == ".md" and p.name != "README.md"),
    ]:
        dst = CLAUDE_HOME / subdir
        # Layer 1: agentihooks built-in
        _symlink_dir_contents(AGENTIHOOKS_ROOT / _CLAUDE_SUBDIR / subdir, dst, label=label, filter_fn=filter_fn)
        # Layer 2: bundle top-level .claude/ (inherits to all profiles)
        if bundle_dir and (bundle_dir / _CLAUDE_SUBDIR / subdir).is_dir():
            _symlink_dir_contents(
                bundle_dir / _CLAUDE_SUBDIR / subdir, dst, label=f"bundle {label}", filter_fn=filter_fn
            )
        # Layer 3+: each profile in chain (later profiles override earlier for same-name files)
        for pname, pdir in profile_dirs:
            if (pdir / _CLAUDE_SUBDIR / subdir).is_dir():
                chain_label = f"profile({pname}) {label}" if len(profile_chain) > 1 else f"profile {label}"
                _symlink_dir_contents(pdir / _CLAUDE_SUBDIR / subdir, dst, label=chain_label, filter_fn=filter_fn)

    # --- 5. Install CLAUDE.md (single profile = copy; chain = concatenated copy) ---
    _cleanup_stale_claude_md_symlink()
    # Remove any previous chain-injected CLAUDE.md rules
    rules_dir = CLAUDE_HOME / "rules"
    if rules_dir.is_dir():
        for f in rules_dir.iterdir():
            if f.name.startswith("_profile-") and f.name.endswith(".md") and f.is_symlink():
                f.unlink()

    if len(profile_chain) == 1:
        # Single profile: copy the profile's CLAUDE.md (real file, not a symlink)
        _install_system_prompt(profile_dirs[0][1], profile_dirs[0][0])
    else:
        # Chain mode: concatenate all CLAUDE.md files into one rendered file
        claude_md_parts: list[str] = []
        for pname, pdir in profile_dirs:
            claude_md_src = pdir / _CLAUDE_MD_NAME
            if claude_md_src.exists():
                content = claude_md_src.read_text().strip()
                if content:
                    claude_md_parts.append(f"<!-- profile: {pname} -->\n{content}")

        if claude_md_parts:
            dst = CLAUDE_HOME / _CLAUDE_MD_NAME
            chained = "\n\n---\n\n".join(claude_md_parts) + "\n"
            up_to_date = False
            # Carry over any third-party fenced block (agentibridge et al) before
            # this write replaces the file wholesale.
            if dst.exists() and not dst.is_symlink():
                _force_backup_if_malformed(dst)
                chained = _preserve_foreign_blocks(chained, dst.read_text())
                # Same up-to-date guard the single-profile writer uses: without it
                # every chained init takes the backup+overwrite path below and
                # drops another CLAUDE.md.bak.<timestamp> into ~/.claude.
                # NB: skip only the write — steps 5a/5b and MCP install still run.
                up_to_date = _same_content(_strip_managed_blocks(dst.read_text()), chained)

            if up_to_date:
                # Back up before claiming ownership: an operator file that merely
                # happens to match would otherwise be deleted by uninstall with
                # "no pre-agentihooks original recorded".
                _claim_claude_md_ownership(dst)
                _cprint(f"  [--] Chained {_CLAUDE_MD_NAME} already up to date")
            else:
                # Remove stale symlink, or back up a pre-existing real file first
                if dst.is_symlink():
                    dst.unlink()
                elif dst.exists():
                    backup = _backup_existing_claude_md(dst)
                    if backup:
                        print(f"  Backed up existing {_CLAUDE_MD_NAME} → {backup}")
                    dst.unlink()
                dst.write_text(chained)
                _record_managed_claude_md(dst)
                sources = [pn for pn, pd in profile_dirs if (pd / _CLAUDE_MD_NAME).exists()]
                _cprint(f"[OK] Wrote chained CLAUDE.md ({' + '.join(sources)})")
        else:
            # No CLAUDE.md in any profile — try last profile as fallback
            _install_system_prompt(profile_dirs[-1][1], profile_dirs[-1][0])

    # --- 5a. Prepend bundle shared CLAUDE.md (cross-profile directives) ---
    # Runs once, outside the single-profile/chain branch, so a chained install gets
    # the shared block exactly once at the top rather than once per profile.
    _prepend_bundle_claude_md(bundle_dir)

    # --- 5b. Append CI manifesto to ~/.claude/CLAUDE.md (memory channel) ---
    _append_ci_manifesto_to_claude_md()

    # --- 6. Install MCP servers to user scope (~/.claude.json) ---
    # Layer 1: hooks-utils from agentihooks
    _install_user_mcp(last_profile)

    # Layer 2: bundle .claude/.mcp.json — always installed; profile MCPs layer on top (override per-name)
    if bundle_dir:
        bundle_mcp = bundle_dir / _CLAUDE_SUBDIR / _MCP_JSON_NAME
        if not bundle_mcp.exists():
            bundle_mcp = bundle_dir / _MCP_JSON_NAME
        if bundle_mcp.exists():
            try:
                mcp_data = load_json(bundle_mcp)
                servers = mcp_data.get("mcpServers", {})
                if servers:
                    _merge_mcp_to_user_scope(servers)
                    _cprint(f"  [OK] Bundle MCP servers: {', '.join(servers.keys())}")
            except (json.JSONDecodeError, OSError) as exc:
                _cprint(f"  [WARN] Could not read bundle .mcp.json: {exc}")

    # Layer 3+: each profile's .mcp.json (chained)
    for pname, pdir in profile_dirs:
        profile_mcp = pdir / _CLAUDE_SUBDIR / _MCP_JSON_NAME
        if profile_mcp.exists():
            try:
                mcp_data = load_json(profile_mcp)
                servers = mcp_data.get("mcpServers", {})
                if servers:
                    _merge_mcp_to_user_scope(servers)
                    chain_label = f"Profile({pname})" if len(profile_chain) > 1 else "Profile"
                    _cprint(f"  [OK] {chain_label} MCP servers: {', '.join(servers.keys())}")
            except (json.JSONDecodeError, OSError) as exc:
                _cprint(f"  [WARN] Could not read profile .mcp.json: {exc}")

    # --- 6b. Settings-profile MCP overlay ---
    if settings_profile_dir is not None:
        sp_mcp = settings_profile_dir / _CLAUDE_SUBDIR / _MCP_JSON_NAME
        if sp_mcp.exists():
            try:
                mcp_data = load_json(sp_mcp)
                servers = mcp_data.get("mcpServers", {})
                if servers:
                    _merge_mcp_to_user_scope(servers)
                    _cprint(f"  [OK] Settings-profile MCP servers: {', '.join(servers.keys())}")
            except (json.JSONDecodeError, OSError) as exc:
                _cprint(f"  [WARN] Could not read settings-profile .mcp.json: {exc}")

    # --- 7. Re-apply any custom MCPs tracked in state.json ---
    if STATE_JSON.exists():
        print()
        sync_user_mcp()

    # --- 9b. Auto-install MCP file from AGENTIHOOKS_MCP_FILE env var ---
    mcp_file_env = os.environ.get("AGENTIHOOKS_MCP_FILE", "")
    if mcp_file_env:
        mcp_path = Path(mcp_file_env).expanduser().resolve()
        if mcp_path.exists():
            print()
            manage_user_mcp(mcp_path)
        else:
            _cprint(f"  [--] AGENTIHOOKS_MCP_FILE={mcp_file_env} not found — skipping.")

    # --- 10. Install agentihooks CLI tool to ~/.local/bin ---
    print()
    _install_cli_tool()

    # --- 11. Seed ~/.agentihooks/.env from .env.example (first run only) ---
    print()
    _seed_user_env_file()

    # --- 12. Register install in state.json ---
    # Persist the OPERATOR-INTENT chain (profile_input), not the shrunk
    # runtime chain. See note above: dropping unresolvable entries from
    # state lets a transient git operation in the bundle repo silently
    # demote the chain to "default".
    _register_target_global(persisted_profile, settings_profile=settings_profile_name)

    # --- 12b. Reconcile the managed-MCP ledger ---
    # Remove servers agentihooks installed on a previous run but that have since
    # been dropped from every profile/bundle source. Runs AFTER
    # _register_target_global so the collector reads the freshly-written chain.
    #
    # Guard: only prune when the FULL intended profile chain resolved this run.
    # A transiently-missing profile source (e.g. a git checkout in the bundle
    # repo briefly removing files) would otherwise shrink current_managed and
    # falsely delete that profile's servers — the same footgun the persisted-
    # intent chain above defends against.
    intended_chain = [p.strip() for p in persisted_profile.split(",") if p.strip()]
    if len(profile_chain) == len(intended_chain):
        current_managed = set(_collect_all_managed_mcp_servers().keys())
        removed_mcp = _reconcile_managed_mcp_ledger(current_managed)
        if removed_mcp:
            _cprint(
                f"  [OK] Removed {len(removed_mcp)} MCP server(s) no longer in any "
                f"profile/bundle: {', '.join(removed_mcp)}"
            )
    else:
        _cprint(
            "  [--] Skipping MCP ledger reconcile — not every profile in the chain "
            "resolved this run (transient source loss); ledger left unchanged."
        )

    _snapshot_claude_json()

    # --- Track version in state.json ---
    state = _load_state()
    state["version"] = _get_version()
    state["installed_at"] = datetime.now(timezone.utc).isoformat()
    _save_state(state)

    # --- Done ---
    print()
    print(f"{_GREEN}{_BOLD}Installation complete.{_RESET}")
    print()
    print(f"{_DIM}Verify:{_RESET}")
    print(f"  {_DIM}ls -la {existing_settings_path}{_RESET}")
    claude_md = CLAUDE_HOME / _CLAUDE_MD_NAME
    if claude_md.is_symlink():
        print(f"  {_DIM}ls -la {claude_md}{_RESET}")
    print()
    print(f"Launch:  {_CYAN}agentihooks claude{_RESET}   {_DIM}# or: agenti (after source ~/.bashrc){_RESET}")
    print(f"Re-run:  {_DIM}agentihooks init{_RESET}")


# ---------------------------------------------------------------------------
# User-scope MCP install (~/.claude.json)
# ---------------------------------------------------------------------------


def _resolve_claude_json() -> Path:
    """Resolve ~/.claude.json, respecting CLAUDE_CODE_HOME_DIR."""
    home_dir = os.environ.get("CLAUDE_CODE_HOME_DIR")
    if home_dir:
        return Path(home_dir) / ".claude.json"
    return Path.home() / ".claude.json"


_CLAUDE_JSON = _resolve_claude_json()


def _get_user_scope_mcp_names() -> set[str]:
    """Return the set of MCP server names defined in ~/.claude.json."""
    if not _CLAUDE_JSON.exists():
        return set()
    try:
        data = load_json(_CLAUDE_JSON)
        return set(data.get("mcpServers", {}).keys())
    except (json.JSONDecodeError, OSError):
        return set()


def _get_all_known_mcp_names() -> set[str]:
    """Return ALL known MCP server names from ~/.claude.json.

    Includes root mcpServers keys AND claudeAiMcpEverConnected entries.
    """
    if not _CLAUDE_JSON.exists():
        return set()
    try:
        data = load_json(_CLAUDE_JSON)
        names = set(data.get("mcpServers", {}).keys())
        names.update(data.get("claudeAiMcpEverConnected", []))
        return names
    except (json.JSONDecodeError, OSError):
        return set()


def _snapshot_claude_json() -> None:
    """Capture key metadata from ~/.claude.json into state.json for drift detection."""
    if not _CLAUDE_JSON.exists():
        return
    try:
        data = load_json(_CLAUDE_JSON)
    except (json.JSONDecodeError, OSError):
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

    state = _load_state()
    state["claude_json_snapshot"] = snapshot
    _save_state(state)


def _reconcile_managed_mcp_ledger(current_managed: set[str]) -> list[str]:
    """Remove MCP servers agentihooks installed before but no longer manages.

    Diffs the persisted ledger (``state['managed_mcp_servers']`` — the server
    names agentihooks wrote on the last install) against *current_managed* (what
    the bundle/profile chain defines now). Any name in the ledger but absent from
    *current_managed* was dropped from a source file, so it is removed from
    ``~/.claude.json``. The ledger is then rewritten to *current_managed*.

    Servers the operator added by hand (``claude mcp add`` or a manual edit) are
    never in the ledger, so they are never touched — this is the conservative
    counterpart to ``agentihooks prune``, which sweeps every unmanaged server
    regardless of provenance.

    Returns the sorted list of removed server names (may be empty).
    """
    state = _load_state()
    old_ledger = set(state.get("managed_mcp_servers", []))
    to_remove = old_ledger - current_managed
    if to_remove:
        _remove_mcp_from_user_scope({name: None for name in to_remove})
    # Reload before rewriting: _remove_mcp_from_user_scope only touches
    # ~/.claude.json, but reload keeps us robust to any state mutation.
    state = _load_state()
    state["managed_mcp_servers"] = sorted(current_managed)
    _save_state(state)
    return sorted(to_remove)


def _merge_mcp_to_user_scope(servers: dict) -> None:
    """Merge *servers* into the top-level mcpServers of ~/.claude.json."""
    existing: dict = load_json(_CLAUDE_JSON) if _CLAUDE_JSON.exists() else {}
    existing_servers: dict = existing.get("mcpServers", {})
    added, updated = [], []
    for name, config in servers.items():
        if name in existing_servers:
            if existing_servers[name] != config:
                updated.append(name)
        else:
            added.append(name)
        existing_servers[name] = config
    existing["mcpServers"] = existing_servers
    save_json(_CLAUDE_JSON, existing)
    if added:
        _cprint(f"  [OK] Added user-scope MCP servers  : {', '.join(added)}")
    if updated:
        _cprint(f"  [OK] Updated user-scope MCP servers: {', '.join(updated)}")
    if not added and not updated:
        _cprint(f"  [--] User-scope MCP servers unchanged: {', '.join(servers.keys())}")


def _remove_mcp_from_user_scope(servers: dict) -> None:
    """Remove *servers* keys from the top-level mcpServers of ~/.claude.json."""
    if not _CLAUDE_JSON.exists():
        _cprint("  [--] ~/.claude.json does not exist — nothing to remove.")
        return
    existing: dict = load_json(_CLAUDE_JSON)
    existing_servers: dict = existing.get("mcpServers", {})
    removed, missing = [], []
    for name in servers:
        if name in existing_servers:
            del existing_servers[name]
            removed.append(name)
        else:
            missing.append(name)
    existing["mcpServers"] = existing_servers
    save_json(_CLAUDE_JSON, existing)
    if removed:
        _cprint(f"  [OK] Removed user-scope MCP servers: {', '.join(removed)}")
    if missing:
        _cprint(f"  [--] Not found (already removed?)  : {', '.join(missing)}")


def _build_mcp_config(mcp_categories: str) -> dict:
    """Build MCP server config for the hooks-utils server.

    The ``command`` path must be a python that can ``import hooks`` from
    any cwd — Claude Code launches the MCP from its own working directory
    and relying on cwd-on-sys.path produces silent ModuleNotFoundError.

    Resolution order:
    1. ``_detect_venv()`` — VIRTUAL_ENV first, then ``~/.agentihooks/.venv``,
       then ``./.venv`` (operator intent).
    2. ``sys.executable`` — the python currently running install.py.

    Each candidate is probed via ``_python_can_import_hooks`` from cwd ``/``;
    the first that succeeds wins. If none pass, install bails with a clear
    error rather than baking a broken path into ``~/.claude.json``.
    """
    candidates: list[Path] = []
    detected = _detect_venv()
    if detected:
        candidates.append(detected)
    sys_exec = Path(sys.executable)
    if sys_exec not in candidates:
        candidates.append(sys_exec)

    chosen: Path | None = None
    failed: list[Path] = []
    for cand in candidates:
        if _python_can_import_hooks(cand):
            chosen = cand
            break
        failed.append(cand)

    if chosen is None:
        msg_parts = [
            "ERROR: no python found that can `import hooks` from a neutral cwd.",
            "Refusing to write a broken hooks-utils MCP command into ~/.claude.json.",
            "Tried:",
        ]
        for p in failed:
            msg_parts.append(f"  - {p}")
        msg_parts.append(
            "Fix: install agentihooks editable into your venv, e.g.\n"
            '  uv pip install --python <path-to-python> -e ".[all]"'
        )
        print("\n".join(msg_parts), file=sys.stderr)
        sys.exit(1)

    return {
        "mcpServers": {
            "hooks-utils": {
                "command": str(chosen),
                "args": ["-m", "hooks.mcp"],
                "cwd": str(AGENTIHOOKS_ROOT),
                "env": {"MCP_CATEGORIES": mcp_categories},
            }
        }
    }


def _install_user_mcp(profile_name: str) -> None:
    """Generate and merge MCP server config into ~/.claude.json.

    Builds the hooks-utils MCP server config with all categories enabled.
    """
    mcp_config = _build_mcp_config("all")
    _merge_mcp_to_user_scope(mcp_config["mcpServers"])


def manage_user_mcp(mcp_path: Path, *, uninstall: bool = False) -> None:
    """Install or uninstall MCP servers from an external file into user scope.

    Reads *mcp_path* (must contain a ``mcpServers`` dict) and either merges
    all servers into ``~/.claude.json`` (install) or removes them (uninstall).
    No path substitution is applied — the file is used as-is.
    """
    if not mcp_path.exists():
        print(f"ERROR: MCP file not found: {mcp_path}", file=sys.stderr)
        sys.exit(1)
    try:
        raw = load_json(mcp_path)
    except (json.JSONDecodeError, OSError) as exc:
        print(f"ERROR: Cannot read {mcp_path}: {exc}", file=sys.stderr)
        sys.exit(1)

    servers: dict = raw.get("mcpServers", {})
    if not servers:
        _cprint(f"  [--] No mcpServers found in {mcp_path} — nothing to do.")
        return

    action = "Uninstalling" if uninstall else "Installing"
    print(f"{action} MCP servers from {mcp_path}:")
    print(f"  Servers: {', '.join(servers.keys())}")
    print()
    if uninstall:
        _remove_mcp_from_user_scope(servers)
        _state_remove_mcp(mcp_path)
    else:
        _merge_mcp_to_user_scope(servers)
        _state_add_mcp(mcp_path)


def _reseed_managed_mcp_sources() -> None:
    """Re-merge bundle + active-profile .mcp.json into ~/.claude.json mcpServers.

    Idempotent. Used by `init` so the source-of-truth files (bundle
    .claude/.mcp.json, profile .claude/.mcp.json, and the hooks-utils
    server) are always present in user scope.
    """
    state = _load_state()
    profile_name = state.get("targets", {}).get("global", {}).get("profile")

    # Layer 1: hooks-utils (driven by profile mcp_categories)
    if profile_name:
        try:
            _install_user_mcp(profile_name)
        except Exception as exc:
            _cprint(f"  [WARN] Could not reseed hooks-utils MCP: {exc}")

    # Layer 2: bundle .mcp.json
    bundle_dir = _get_bundle_path()
    if bundle_dir:
        for candidate in (bundle_dir / _CLAUDE_SUBDIR / _MCP_JSON_NAME, bundle_dir / _MCP_JSON_NAME):
            if candidate.exists():
                try:
                    servers = load_json(candidate).get("mcpServers", {})
                    if servers:
                        _merge_mcp_to_user_scope(servers)
                except (json.JSONDecodeError, OSError) as exc:
                    _cprint(f"  [WARN] Could not reseed bundle .mcp.json: {exc}")
                break

    # Layer 3: active profile .mcp.json (full chain — see collector note)
    if profile_name:
        for _pname, profile_dir in _resolve_profile_chain(profile_name):
            profile_mcp = profile_dir / _CLAUDE_SUBDIR / _MCP_JSON_NAME
            if profile_mcp.exists():
                try:
                    servers = load_json(profile_mcp).get("mcpServers", {})
                    if servers:
                        _merge_mcp_to_user_scope(servers)
                except (json.JSONDecodeError, OSError) as exc:
                    _cprint(f"  [WARN] Could not reseed profile .mcp.json: {exc}")

    # Layer 3b: settings-profile overlay .mcp.json
    settings_profile = state.get("targets", {}).get("global", {}).get("settings_profile")
    if settings_profile:
        sp_dir = _resolve_profile_dir(settings_profile)
        if sp_dir:
            sp_mcp = sp_dir / _CLAUDE_SUBDIR / _MCP_JSON_NAME
            if sp_mcp.exists():
                try:
                    servers = load_json(sp_mcp).get("mcpServers", {})
                    if servers:
                        _merge_mcp_to_user_scope(servers)
                except (json.JSONDecodeError, OSError) as exc:
                    _cprint(f"  [WARN] Could not reseed settings-profile .mcp.json: {exc}")


def sync_user_mcp() -> None:
    """Re-apply MCP source-of-truth files into ~/.claude.json mcpServers.

    Order:
    1. Reseed bundle + active-profile .mcp.json + hooks-utils (managed sources)
    2. Re-merge user-tracked .mcp.json files from ~/.agentihooks/state.json mcpFiles

    Skips paths that no longer exist (with a warning) so a missing
    repo doesn't abort the whole sync.
    """
    _reseed_managed_mcp_sources()

    state = _load_state()
    paths: list[str] = state.get("mcpFiles", [])
    if not paths:
        _cprint(f"  [--] No MCP files tracked in {STATE_JSON} — nothing to sync.")
        return

    print(f"Syncing {len(paths)} tracked MCP file(s) from {STATE_JSON}:")
    for path_str in paths:
        p = Path(path_str)
        if not p.exists():
            _cprint(f"  [!!] Skipping missing file: {path_str}")
            continue
        try:
            raw = load_json(p)
        except (json.JSONDecodeError, OSError) as exc:
            _cprint(f"  [!!] Cannot read {path_str}: {exc}")
            continue
        servers: dict = raw.get("mcpServers", {})
        if not servers:
            _cprint(f"  [--] No mcpServers in {path_str} — skipping.")
            continue
        print(f"  From {p.name}: {', '.join(servers.keys())}")
        _merge_mcp_to_user_scope(servers)


# ---------------------------------------------------------------------------
# Interactive MCP uninstall (--mcp --uninstall without a path)
# ---------------------------------------------------------------------------


def _cmd_mcp_interactive_uninstall() -> None:
    """Show tracked MCP files, let user pick one, uninstall its servers."""
    state = _load_state()
    paths: list[str] = state.get("mcpFiles", [])

    if not paths:
        print(f"No MCP files tracked in {STATE_JSON} — nothing to uninstall.")
        return

    print("Tracked MCP files:\n")
    for i, path_str in enumerate(paths, 1):
        p = Path(path_str)
        if not p.exists():
            print(f"  {i}. {path_str}  [file not found]")
            continue
        try:
            servers = load_json(p).get("mcpServers", {})
            names = ", ".join(servers.keys()) if servers else "(no servers)"
            print(f"  {i}. {path_str}")
            print(f"     {len(servers)} server(s): {names}")
        except (json.JSONDecodeError, OSError):
            print(f"  {i}. {path_str}  [unreadable]")

    print()
    try:
        raw = input(f"Select file to uninstall [1-{len(paths)}] (or q to quit): ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\nAborted.")
        sys.exit(0)

    if raw.lower() == "q":
        print("Aborted.")
        sys.exit(0)

    try:
        idx = int(raw) - 1
        if not 0 <= idx < len(paths):
            raise ValueError
    except ValueError:
        print("Invalid selection.")
        sys.exit(1)

    selected = Path(paths[idx])
    print()
    manage_user_mcp(selected, uninstall=True)
    print()
    print("Restart Claude Code for the changes to take effect.")


# ---------------------------------------------------------------------------
# --mcp-lib: browse a directory of .mcp.json files and install one
# ---------------------------------------------------------------------------


def _cmd_mcp_lib(lib_path: Path | None) -> None:
    """Scan a directory for .mcp.json files and interactively install one."""
    if lib_path is None:
        lib_path = _state_get_mcp_lib()
        if lib_path is None:
            print("No MCP library path set. Provide one:")
            print("  agentihooks --mcp-lib /path/to/dir")
            sys.exit(1)
        print(f"Using saved MCP library: {lib_path}")

    lib_path = lib_path.expanduser().resolve()
    if not lib_path.is_dir():
        _cprint(f"[!!] Not a directory: {lib_path}", file=sys.stderr)
        sys.exit(1)

    # Keep only files that contain mcpServers
    mcp_files: list[tuple[Path, dict]] = []
    for f in sorted(lib_path.glob("*.json")):
        try:
            data = load_json(f)
            if "mcpServers" in data:
                mcp_files.append((f, data["mcpServers"]))
        except (json.JSONDecodeError, OSError):
            pass

    if not mcp_files:
        print(f"No .json files with mcpServers found in {lib_path}")
        sys.exit(0)

    # Save path for future --mcp-lib calls
    _state_set_mcp_lib(lib_path)

    already_tracked = set(_load_state().get("mcpFiles", []))

    print(f"MCP files in {lib_path}:\n")
    for i, (f, servers) in enumerate(mcp_files, 1):
        names = ", ".join(servers.keys())
        tracked = "  [installed]" if str(f) in already_tracked else ""
        print(f"  {i}. {f.name}{tracked}")
        print(f"     {len(servers)} server(s): {names}")

    print()
    try:
        raw = input(f"Select file to install [1-{len(mcp_files)}] (or q to quit): ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\nAborted.")
        sys.exit(0)

    if raw.lower() == "q":
        print("Aborted.")
        sys.exit(0)

    try:
        idx = int(raw) - 1
        if not 0 <= idx < len(mcp_files):
            raise ValueError
    except ValueError:
        print("Invalid selection.")
        sys.exit(1)

    selected, _ = mcp_files[idx]
    print()
    manage_user_mcp(selected)
    print()
    print("Restart Claude Code for the changes to take effect.")


# ---------------------------------------------------------------------------
# CLI tool install (uv tool install --editable .)
# ---------------------------------------------------------------------------

_CLI_NAME = "agentihooks"


def _install_cli_tool() -> None:
    """Install the agentihooks CLI globally via ``uv tool install --editable .``."""
    import subprocess

    uv = shutil.which("uv")
    if not uv:
        _cprint("  [!!] uv not found — install uv first: https://docs.astral.sh/uv/getting-started/installation/")
        print("       Then re-run: uv run agentihooks init")
        return

    if _is_source_checkout():
        cmd = [uv, "tool", "install", "--editable", "--force", "."]
        cwd = str(AGENTIHOOKS_ROOT)
        label = "uv tool install --editable ."
    else:
        cmd = [uv, "tool", "install", "--force", "agentihooks"]
        cwd = None
        label = "uv tool install agentihooks"
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if result.returncode == 0:
        _cprint(f"  [OK] CLI installed via: {label}")
    else:
        _cprint(f"  [!!] uv tool install failed: {result.stderr.strip()}")


def _uninstall_cli_tool() -> None:
    """Uninstall the agentihooks CLI via ``uv tool uninstall``."""
    import subprocess

    uv = shutil.which("uv")
    if not uv:
        _cprint("  [!!] uv not found — cannot uninstall CLI automatically.")
        print(f"       Remove manually: uv tool uninstall {_CLI_NAME}")
        return

    result = subprocess.run(
        [uv, "tool", "uninstall", _CLI_NAME],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        _cprint(f"  [OK] Uninstalled CLI via: uv tool uninstall {_CLI_NAME}")
    else:
        stderr = result.stderr.strip()
        if "not installed" in stderr.lower():
            _cprint(f"  [--] {_CLI_NAME} was not installed via uv tool (skipping)")
        else:
            _cprint(f"  [!!] uv tool uninstall failed: {stderr}")


# ---------------------------------------------------------------------------
# Symlink helpers
# ---------------------------------------------------------------------------


def _remove_agentihooks_symlinks(dst_dir: Path, label: str) -> int:
    """Remove symlinks in *dst_dir* managed by agentihooks (built-in or bundle).

    Returns the count of removed links. Non-symlinks and symlinks pointing
    elsewhere are left untouched (user-created links stay safe).
    """
    if not dst_dir.exists():
        return 0
    count = 0
    managed_roots = [str(AGENTIHOOKS_ROOT)]
    bundle = _get_bundle_path()
    if bundle:
        managed_roots.append(str(bundle))
    for link in sorted(dst_dir.iterdir()):
        if not link.is_symlink():
            continue
        try:
            target = str(link.resolve())
        except OSError:
            continue
        if any(target.startswith(root) for root in managed_roots):
            link.unlink()
            _cprint(f"  [RM] Removed {label} symlink: {link.name}")
            count += 1
    return count


def _cleanup_stale_links(dst_dir: Path, src_dir: Path, filter_fn: Callable[[Path], bool] | None) -> None:
    """Remove broken symlinks and symlinks that no longer pass *filter_fn*."""
    if not dst_dir.exists():
        return
    for link in sorted(dst_dir.iterdir()):
        if not link.is_symlink():
            continue
        target = link.resolve()
        if not link.exists():
            link.unlink()
            _cprint(f"  [RM] Removed broken symlink: {link.name}")
        elif target.parent.resolve() == src_dir.resolve() and filter_fn and not filter_fn(target):
            link.unlink()
            _cprint(f"  [RM] Removed stale symlink: {link.name}")


def _link_item(item: Path, link: Path, label: str) -> None:
    """Create or update a single symlink *link* → *item*."""
    if link.is_symlink():
        if link.resolve() == item.resolve():
            _cprint(f"  [--] {label} '{item.name}' already linked → {item}")
        else:
            link.unlink()
            link.symlink_to(item)
            _cprint(f"  [OK] Re-linked {label} '{item.name}' → {item}")
    elif link.exists():
        _cprint(f"  [!!] {label} '{item.name}' exists at {link} and is not a symlink – skipping (remove manually)")
    else:
        link.symlink_to(item)
        _cprint(f"  [OK] Linked {label} '{item.name}' → {item}")


def _symlink_dir_contents(
    src_dir: Path,
    dst_dir: Path,
    *,
    label: str,
    filter_fn: Callable[[Path], bool] | None = None,
) -> None:
    """Symlink filtered children of *src_dir* into *dst_dir*.

    Stale symlinks (broken or pointing to items that no longer pass the filter)
    are removed automatically before new links are created.
    """
    if not src_dir.exists():
        print(f"  (no {label}s directory at {src_dir}, skipping)")
        return

    _cleanup_stale_links(dst_dir, src_dir, filter_fn)

    children = [c for c in src_dir.iterdir() if not filter_fn or filter_fn(c)]
    if not children:
        print(f"  (no valid {label}s found in {src_dir} after filtering, skipping)")
        return

    dst_dir.mkdir(parents=True, exist_ok=True)
    for item in sorted(children):
        if not item.name.startswith("."):
            _link_item(item, dst_dir / item.name, label)


def _append_ci_manifesto_to_claude_md() -> None:
    """Append the CI manifesto to ~/.claude/CLAUDE.md as a fenced block.

    The manifesto used to be injected at SessionStart via stdout, but Claude
    Code's hook output is capped at ~2KB before the harness substitutes the
    body with a filepath preview. The manifesto is ~36KB, so the model never
    saw the doctrine. CLAUDE.md is loaded through the memory channel, which
    has no such cap — appending the manifesto here puts it permanently in
    the model's context for every session, at zero per-session cost.

    Idempotent: if the manifesto block is already present and unchanged,
    no write happens.
    """
    try:
        from hooks import config as _cfg
    except Exception:
        return
    if not getattr(_cfg, "CI_MANIFESTO_ENABLED", True):
        return
    manifesto_path = Path(getattr(_cfg, "CI_MANIFESTO_PATH", "")).expanduser()
    if not manifesto_path.exists():
        _cprint(f"  [--] CI manifesto not found at {manifesto_path} — skipping CLAUDE.md append.")
        return
    dst = CLAUDE_HOME / _CLAUDE_MD_NAME
    if not dst.exists():
        # Nothing to append to — install_system_prompt handles its own write
        return
    body = manifesto_path.read_text().rstrip()
    block = (
        "\n\n<!-- BEGIN CI MANIFESTO (auto-injected by agentihooks init) -->\n"
        f"<!-- Source: {manifesto_path} -->\n\n"
        f"{body}\n\n"
        "<!-- END CI MANIFESTO -->\n"
    )
    current = dst.read_text()
    begin_marker = "<!-- BEGIN CI MANIFESTO"
    end_marker = "<!-- END CI MANIFESTO -->"
    if begin_marker in current and end_marker in current:
        # Replace existing block
        before = current.split(begin_marker, 1)[0].rstrip()
        after_split = current.split(end_marker, 1)
        after = after_split[1] if len(after_split) > 1 else ""
        new_content = before + block + after.lstrip("\n")
        if new_content == current:
            _cprint("  [--] CI manifesto block already up to date in CLAUDE.md")
            return
    else:
        new_content = current.rstrip() + block
    # The block text contains _CLAUDE_MD_MANAGED_MARKER, so appending it is itself
    # an ownership claim — uninstall will delete a file carrying it. Capture the
    # operator's original first. Reached when no profile supplied a CLAUDE.md and
    # no bundle block was prepended, so nothing earlier took a backup.
    _claim_claude_md_ownership(dst)
    dst.write_text(new_content)
    _cprint(f"  [OK] Appended CI manifesto to {dst} ({len(body):,} bytes)")


# Markers for the optional bundle-wide CLAUDE.md, prepended ahead of all profile
# content. Two constraints on the wording:
#   1. Avoid the substring "profile:" — the init lost-state guard regex-scans the
#      whole file for `<!-- profile: NAME -->` and would read it as a phantom
#      chain member.
#   2. Avoid _CLAUDE_MD_MANAGED_MARKER ("auto-injected by agentihooks init").
#      _claude_md_is_managed() treats that substring as proof agentihooks owns the
#      file. A bundle block alone must never make an operator's hand-authored
#      CLAUDE.md look managed — uninstall deletes managed files, and a file we
#      merely prepended to has no recorded original to restore from.
_BUNDLE_CLAUDE_MD_BEGIN = "<!-- BEGIN BUNDLE CLAUDE.md (managed block — edit the bundle, not here) -->"
_BUNDLE_CLAUDE_MD_END = "<!-- END BUNDLE CLAUDE.md -->"

# Every marker that delimits a block install writes into ~/.claude/CLAUDE.md.
# Bundle content may not contain any of them: the splices below are
# first-occurrence based, so an embedded copy silently eats real content.
_CI_MANIFESTO_BEGIN_PREFIX = "<!-- BEGIN CI MANIFESTO"
_CI_MANIFESTO_END = "<!-- END CI MANIFESTO -->"
_MANAGED_BLOCK_MARKERS = (
    _BUNDLE_CLAUDE_MD_BEGIN,
    _BUNDLE_CLAUDE_MD_END,
    _CI_MANIFESTO_BEGIN_PREFIX,
    _CI_MANIFESTO_END,
)


# Fenced blocks in CLAUDE.md — `<!-- BEGIN name -->…<!-- END name -->`. agentihooks
# owns two of them; agentibridge and anything else may own others. The profile
# writer replaces the file wholesale, so foreign blocks must be carried across or
# they are collateral damage on every init.
#
# This is scanned with a depth-tracking walk rather than a regex splice. Flat
# "find first BEGIN, cut to nearest END" surgery silently loses content on nested
# blocks, interleaved blocks, and owned-looking markers embedded inside a foreign
# block — all of which are one documentation example away from happening.
_BLOCK_MARKER_RE = re.compile(r"<!--\s*(?P<kind>BEGIN|END)\s+(?P<rest>[^\n]*?)\s*-->")

_OWNED_BLOCK_NAMES = frozenset({"CI MANIFESTO", "BUNDLE CLAUDE.md"})


def _marker_name(rest: str) -> str:
    """Normalise a marker's trailing text to a block name.

    Owned BEGIN markers carry a parenthetical the matching END does not, so they
    are folded to a bare name before pairing.
    """
    for owned in _OWNED_BLOCK_NAMES:
        if rest.startswith(owned):
            return owned
    return rest


def _scan_top_level_blocks(text: str) -> tuple[list[tuple[str, int, int]], bool]:
    """Return ``([(name, start, end)], unbalanced)`` for depth-0 fenced blocks.

    Only blocks that open and close at the top level are reported, so markers
    nested inside another block are left alone. ``unbalanced`` is True when a
    BEGIN never closes or an END has no opener — the caller warns rather than
    silently dropping the region.
    """
    stack: list[tuple[str, int]] = []
    blocks: list[tuple[str, int, int]] = []
    unbalanced = False

    for m in _BLOCK_MARKER_RE.finditer(text):
        name = _marker_name(m.group("rest"))
        if m.group("kind") == "BEGIN":
            stack.append((name, m.start()))
            continue
        for i in range(len(stack) - 1, -1, -1):
            if stack[i][0] == name:
                open_name, open_at = stack[i]
                if i != len(stack) - 1:
                    unbalanced = True  # inner blocks left unclosed
                del stack[i:]
                if not stack:
                    blocks.append((open_name, open_at, m.end()))
                break
        else:
            unbalanced = True  # END with no matching BEGIN

    if stack:
        unbalanced = True
    return blocks, unbalanced


def _extract_foreign_blocks(text: str) -> list[tuple[str, str]]:
    """Return ``[(name, block_text)]`` for top-level blocks agentihooks does not own.

    Document order, first occurrence per name, markers included.
    """
    blocks, unbalanced = _scan_top_level_blocks(text)
    if unbalanced:
        _cprint(
            f"  [!!] Unbalanced BEGIN/END markers in {_CLAUDE_MD_NAME} — "
            "a third-party block may be malformed; preserving what pairs cleanly."
        )
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for name, start, end in blocks:
        if name in _OWNED_BLOCK_NAMES or name in seen:
            continue
        seen.add(name)
        out.append((name, text[start:end].strip()))
    return out


def _preserve_foreign_blocks(body: str, current: str) -> str:
    """Append third-party fenced blocks from *current* that *body* lacks.

    Deduplicated by block name against the incoming body. Without that check a
    profile or bundle file that merely *documents* the marker convention would
    have its example re-appended on every init, growing the file without bound.
    """
    incoming = {name for name, _ in _extract_foreign_blocks(body)}
    carried = [text for name, text in _extract_foreign_blocks(current) if name not in incoming]
    if not carried:
        return body
    return body.rstrip("\n") + "\n\n" + "\n\n".join(carried) + "\n"


def _strip_managed_blocks(text: str) -> str:
    """Return *text* with agentihooks' own top-level blocks removed.

    Lets the profile writer compare like with like: without it the "already up to
    date" check compares profile-only content against a file that also carries the
    managed blocks, never matches, and takes the backup+overwrite path on every
    run — littering ~/.claude with a CLAUDE.md.bak.<timestamp> each time.

    Scoped to depth 0, so an owned-looking marker inside somebody else's block is
    left untouched.
    """
    blocks, _ = _scan_top_level_blocks(text)
    owned = [(s, e) for name, s, e in blocks if name in _OWNED_BLOCK_NAMES]
    for start, end in reversed(owned):
        text = text[:start] + text[end:]
    return text


def _claim_claude_md_ownership(dst: Path) -> None:
    """Record agentihooks as owner of *dst*, backing up an unowned file first.

    Uninstall deletes a managed CLAUDE.md outright when no original is recorded,
    so claiming ownership without first capturing the operator's file is silent
    data loss. Every path that records ownership must go through here.
    """
    if dst.exists() and not dst.is_symlink() and not _claude_md_is_managed(dst):
        backup = _backup_existing_claude_md(dst)
        if backup:
            _cprint(f"  [OK] Backed up pre-existing {_CLAUDE_MD_NAME} → {backup.name}")
    _record_managed_claude_md(dst)


def _force_backup_if_malformed(dst: Path) -> None:
    """Back up *dst* unconditionally when its fenced blocks don't pair cleanly.

    Interleaved or never-closed markers are ambiguous — there is no correct way
    to carry them across a rewrite. Rather than guess, guarantee the operator can
    get the content back.
    """
    if not dst.exists() or dst.is_symlink():
        return
    if not _scan_top_level_blocks(dst.read_text())[1]:
        return
    backup = _backup_existing_claude_md(dst)
    if backup:
        _cprint(f"  [!!] Malformed block markers — backed up {_CLAUDE_MD_NAME} → {backup.name}")


def _same_content(a: str, b: str) -> bool:
    """Compare two CLAUDE.md bodies ignoring incidental whitespace.

    Removing a block leaves blank lines behind that the freshly-composed body
    does not have; comparing raw would report "changed" on every run.
    """
    norm = lambda s: "\n".join(ln.rstrip() for ln in s.split("\n") if ln.strip())  # noqa: E731
    return norm(a) == norm(b)


def _prepend_bundle_claude_md(bundle_dir: Path | None) -> None:
    """Prepend the bundle's optional ``<bundle>/.claude/CLAUDE.md`` to ~/.claude/CLAUDE.md.

    Lets an operator write cross-profile directives once in the bundle instead of
    duplicating them into every profile's CLAUDE.md. The block lands ahead of all
    profile content, so a profile can still override shared guidance simply by
    coming later in the file.

    No-op when: no bundle is linked, the bundle has no ``.claude/CLAUDE.md``, that
    file is empty, or ~/.claude/CLAUDE.md does not exist yet (the profile writer
    owns creating the base file — there is nothing to prepend onto).

    Idempotent: replaces a previously-prepended block in place rather than stacking.
    Called unconditionally after both the single-profile and chain branches, and
    re-derives its content from ``(bundle_md on disk, current dst)`` every run — so
    a newly added or changed bundle file lands even when ``_install_system_prompt``
    took its "already up to date" early return.
    """
    dst = CLAUDE_HOME / _CLAUDE_MD_NAME
    # Defensive: step 5 always leaves a real file, but never write through a symlink
    # into a profile source in the repo.
    if not dst.exists() or dst.is_symlink():
        return

    current = dst.read_text()
    had_block = _BUNDLE_CLAUDE_MD_BEGIN in current and _BUNDLE_CLAUDE_MD_END in current

    bundle_md = (bundle_dir / _CLAUDE_SUBDIR / _CLAUDE_MD_NAME) if bundle_dir else None
    body = ""
    if bundle_md and bundle_md.exists():
        body = bundle_md.read_text().strip()

    if not body:
        # Bundle unlinked, file removed, or emptied. Drop a previously-prepended
        # block so the shared directives stop applying instead of being stranded
        # in the file with no way to remove them.
        if had_block:
            head, _, rest = current.partition(_BUNDLE_CLAUDE_MD_BEGIN)
            _, _, tail = rest.partition(_BUNDLE_CLAUDE_MD_END)
            dst.write_text(head + tail.lstrip("\n"))
            _cprint(f"  [OK] Removed stale bundle {_CLAUDE_MD_NAME} block (no bundle content)")
        return

    # First-occurrence splices are used both here and by the manifesto appender,
    # so an embedded marker inside bundle prose would silently eat real content
    # or duplicate on every run. Refuse rather than corrupt.
    for marker in _MANAGED_BLOCK_MARKERS:
        if marker in body:
            _cprint(
                f"  [!!] Bundle {_CLAUDE_MD_NAME} contains the managed marker {marker!r} — "
                f"skipping prepend. Remove it from {bundle_md} (quote it differently)."
            )
            return

    block = f"{_BUNDLE_CLAUDE_MD_BEGIN}\n<!-- Source: {bundle_md} -->\n\n{body}\n\n{_BUNDLE_CLAUDE_MD_END}\n\n"

    if had_block:
        after = current.split(_BUNDLE_CLAUDE_MD_END, 1)[1].lstrip("\n")
    else:
        # About to mutate a file this function did not create. If nothing else has
        # claimed ownership, capture the operator's original first — uninstall
        # deletes a managed file outright when no original was recorded.
        after = current
        _claim_claude_md_ownership(dst)

    new_content = block + after

    if new_content == current:
        _cprint("  [--] Bundle CLAUDE.md block already up to date")
        return

    dst.write_text(new_content)
    _cprint(f"  [OK] Prepended bundle {_CLAUDE_MD_NAME} ({len(body):,} bytes) from {bundle_md}")


def _install_system_prompt(profile_dir: Path, profile_name: str) -> None:
    """Copy profile's CLAUDE.md to ~/.claude/CLAUDE.md.

    Writes a real file (not a symlink) so it resolves correctly across
    WSL/Windows boundaries and VS Code \\\\wsl.localhost paths.
    Re-run ``agentihooks init`` to refresh after editing the profile source.
    """
    src = profile_dir / _CLAUDE_MD_NAME
    dst = CLAUDE_HOME / _CLAUDE_MD_NAME

    if not src.exists():
        _cprint(f"  [--] No {_CLAUDE_MD_NAME} in profile '{profile_name}' — skipping system prompt.")
        return

    # Prepend the same `<!-- profile: name -->` marker the chain writer uses
    # (see the claude_md_parts loop above) so the init guard can sniff the
    # installed profile from single-profile installs too — the common shape.
    # This only touches the rendered dst content; src on disk is never mutated.
    new_content = f"<!-- profile: {profile_name} -->\n{src.read_text()}"

    # Carry over any third-party fenced block (agentibridge et al). This write
    # replaces the whole file, so without this it silently destroys a neighbour's
    # content that only its own installer can put back.
    if dst.exists() and not dst.is_symlink():
        _force_backup_if_malformed(dst)
        new_content = _preserve_foreign_blocks(new_content, dst.read_text())

    # Check if content is already up to date. Compare against the file with the
    # managed blocks (bundle prepend, CI manifesto) stripped — they are appended
    # by later steps, so comparing raw would never match once either exists and
    # every re-run would take the backup+overwrite path below.
    if dst.exists() and not dst.is_symlink():
        if _same_content(_strip_managed_blocks(dst.read_text()), new_content):
            _cprint(f"  [--] {_CLAUDE_MD_NAME} already up to date (from {profile_name})")
            return

    # Remove stale symlink or backup existing file
    if dst.is_symlink():
        dst.unlink()
    elif dst.exists():
        backup = _backup_existing_claude_md(dst)
        if backup:
            print(f"  Backed up existing {_CLAUDE_MD_NAME} → {backup}")
        dst.unlink()

    dst.write_text(new_content)
    _record_managed_claude_md(dst)
    _cprint(f"  [OK] Wrote {_CLAUDE_MD_NAME} (from {profile_name})")


def _cleanup_stale_claude_md_symlink() -> None:
    """Remove ~/.claude/CLAUDE.md if it is a stale profile symlink."""
    claude_md = CLAUDE_HOME / _CLAUDE_MD_NAME
    if not claude_md.is_symlink():
        return
    target_str = str(claude_md.resolve())
    # Remove if it points into any profiles/ directory (built-in or bundle)
    if "profiles/" in target_str or "profiles\\" in target_str:
        claude_md.unlink()
        _cprint(f"  [OK] Removed stale {_CLAUDE_MD_NAME} symlink → {target_str}")


# Marker embedded by _append_ci_manifesto_to_claude_md — also used as a
# content-based ownership signal when state.json predates managed_claude_md.
_CLAUDE_MD_MANAGED_MARKER = "auto-injected by agentihooks init"


def _record_managed_claude_md(dst: Path) -> None:
    """Record that agentihooks owns ~/.claude/CLAUDE.md.

    install now writes CLAUDE.md as a real file (WSL/Windows path safety), so
    uninstall can no longer rely on ``is_symlink()`` to recognise it. Persist
    the path in state.json so uninstall removes exactly what install wrote.
    """
    state = _load_state()
    if state.get("managed_claude_md") != str(dst):
        state["managed_claude_md"] = str(dst)
        _save_state(state)


def _backup_existing_claude_md(dst: Path) -> Path | None:
    """Back up a pre-existing real ~/.claude/CLAUDE.md before install overwrites it.

    The profile CLAUDE.md is written into home and the CI manifesto is appended
    additively on top, so the installed file is not a symlink and cannot be
    recovered by unlinking. Capture the file agentihooks is about to replace and,
    on the FIRST agentihooks install only, record it as the user's original so
    uninstall can restore the machine to its pre-agentihooks state.
    """
    if not dst.exists() or dst.is_symlink():
        return None
    # Second granularity collides on fast successive writes and shutil.copy2 would
    # clobber the previous generation — including the one recorded as the user's
    # original. Never overwrite an existing backup.
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = dst.with_suffix(f".md.bak.{timestamp}")
    suffix = 1
    while backup.exists():
        backup = dst.with_suffix(f".md.bak.{timestamp}_{suffix}")
        suffix += 1
    shutil.copy2(dst, backup)
    state = _load_state()
    # First install: nothing managed yet and no original recorded → this backup
    # is the genuine user-authored original. Never overwrite it on re-installs.
    if "managed_claude_md" not in state and "claude_md_original_backup" not in state:
        state["claude_md_original_backup"] = str(backup)
        _save_state(state)
    return backup


def _claude_md_is_managed(p: Path) -> bool:
    """True if ~/.claude/CLAUDE.md was installed by agentihooks.

    Handles three cases: legacy symlink into a profiles/ dir, a real file
    recorded in state.json, or a real file carrying the manifesto marker
    (fallback for installs whose state.json predates managed_claude_md).
    """
    if p.is_symlink():
        target = str(p.resolve())
        return "profiles/" in target or "profiles\\" in target
    if not p.exists():
        return False
    if _load_state().get("managed_claude_md") == str(p):
        return True
    try:
        return _CLAUDE_MD_MANAGED_MARKER in p.read_text()
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Uninstall
# ---------------------------------------------------------------------------


def _collect_all_managed_mcp_servers() -> dict:
    """Return the union of all MCP servers managed by agentihooks.

    Collects servers from:
    1. The hooks-utils server (generated from profile mcp_categories)
    2. Bundle .claude/.mcp.json (or root .mcp.json)
    3. Profile .claude/.mcp.json
    4. All files tracked in ~/.agentihooks/state.json mcpFiles

    Returns a merged {name: config} dict.
    """
    merged: dict = {}

    # --- 1. hooks-utils server ---
    mcp_config = _build_mcp_config("all")
    merged.update(mcp_config["mcpServers"])

    # --- 2. Bundle .mcp.json ---
    bundle_dir = _get_bundle_path()
    if bundle_dir:
        for candidate in [bundle_dir / _CLAUDE_SUBDIR / _MCP_JSON_NAME, bundle_dir / _MCP_JSON_NAME]:
            if candidate.exists():
                try:
                    merged.update(load_json(candidate).get("mcpServers", {}))
                except (json.JSONDecodeError, OSError):
                    pass
                break

    # --- 3. Profile .mcp.json (full chain) ---
    # NOTE: the active profile is a comma-joined chain (e.g. "anton,brain").
    # Resolve every hop via _resolve_profile_chain — passing the joined string
    # straight to _resolve_profile_dir returns None and silently drops every
    # profile's MCP servers, collapsing the managed set to just hooks-utils.
    state = _load_state()
    global_target = state.get("targets", {}).get("global", {})
    profile_name = global_target.get("profile")
    if profile_name:
        for _pname, profile_dir in _resolve_profile_chain(profile_name):
            profile_mcp = profile_dir / _CLAUDE_SUBDIR / _MCP_JSON_NAME
            if profile_mcp.exists():
                try:
                    merged.update(load_json(profile_mcp).get("mcpServers", {}))
                except (json.JSONDecodeError, OSError):
                    pass

    # --- 3b. Settings-profile overlay .mcp.json ---
    settings_profile = global_target.get("settings_profile")
    if settings_profile:
        sp_dir = _resolve_profile_dir(settings_profile)
        if sp_dir:
            sp_mcp = sp_dir / _CLAUDE_SUBDIR / _MCP_JSON_NAME
            if sp_mcp.exists():
                try:
                    merged.update(load_json(sp_mcp).get("mcpServers", {}))
                except (json.JSONDecodeError, OSError):
                    pass

    # --- 4. State-tracked MCP files ---
    for path_str in state.get("mcpFiles", []):
        p = Path(path_str)
        if not p.exists():
            continue
        try:
            merged.update(load_json(p).get("mcpServers", {}))
        except (json.JSONDecodeError, OSError):
            pass

    return merged


def uninstall_global(args: argparse.Namespace) -> None:
    """Remove all agentihooks artifacts installed by 'install global'."""
    yes: bool = args.yes

    settings_path = CLAUDE_HOME / "settings.json"
    skills_dir = CLAUDE_HOME / "skills"
    agents_dir = CLAUDE_HOME / "agents"
    commands_dir = CLAUDE_HOME / "commands"
    rules_dir = CLAUDE_HOME / "rules"
    claude_md_dst = CLAUDE_HOME / _CLAUDE_MD_NAME
    # --- Audit ---
    remove_settings = False
    if settings_path.exists():
        try:
            remove_settings = load_json(settings_path).get(MANAGED_BY_KEY) == MANAGED_BY_VALUE
        except (json.JSONDecodeError, OSError):
            pass

    def _count_managed_symlinks(d: Path) -> int:
        if not d.exists():
            return 0
        managed_roots = [str(AGENTIHOOKS_ROOT)]
        bundle = _get_bundle_path()
        if bundle:
            managed_roots.append(str(bundle))
        return sum(
            1
            for lnk in d.iterdir()
            if lnk.is_symlink() and any(str(lnk.resolve()).startswith(r) for r in managed_roots)
        )

    n_skills = _count_managed_symlinks(skills_dir)
    n_agents = _count_managed_symlinks(agents_dir)
    n_commands = _count_managed_symlinks(commands_dir)
    n_rules = _count_managed_symlinks(rules_dir)

    # Remove CLAUDE.md whether it's a legacy profiles/ symlink or a real file
    # written by install (tracked in state.json / manifesto marker).
    remove_claude_md = _claude_md_is_managed(claude_md_dst)
    claude_md_is_symlink = claude_md_dst.is_symlink()

    # Servers to remove = current managed set UNION the persisted ledger (catches
    # servers dropped from a profile before uninstall), restricted to what is
    # actually present in ~/.claude.json.
    managed_servers = _collect_all_managed_mcp_servers()
    for name in _load_state().get("managed_mcp_servers", []):
        managed_servers.setdefault(name, None)
    installed_names = _get_user_scope_mcp_names()
    managed_servers = {k: v for k, v in managed_servers.items() if k in installed_names}

    # Early exit if nothing to do
    total_work = (
        int(remove_settings) + n_skills + n_agents + n_commands + n_rules + int(remove_claude_md) + len(managed_servers)
    )
    if total_work == 0:
        print("Nothing to uninstall — agentihooks is not installed.")
        return

    # --- Summary ---
    print("agentihooks uninstall")
    print("======================")
    print("Will remove:")
    if remove_settings:
        print(f"  {settings_path}  (managed by agentihooks)")
    else:
        print(f"  {settings_path}  [SKIP — not managed or not found]")
    print(f"  {skills_dir}/  → {n_skills} symlink(s)")
    print(f"  {agents_dir}/  → {n_agents} symlink(s)")
    print(f"  {commands_dir}/  → {n_commands} symlink(s)")
    print(f"  {rules_dir}/  → {n_rules} symlink(s)")
    if remove_claude_md:
        _orig = _load_state().get("claude_md_original_backup")
        if _orig and Path(_orig).exists():
            print(f"  {claude_md_dst}  (restore pre-agentihooks original ← {Path(_orig).name})")
        elif claude_md_is_symlink:
            print(f"  {claude_md_dst}  (remove symlink → profiles/)")
        else:
            print(f"  {claude_md_dst}  (remove managed file)")
    else:
        print(f"  {claude_md_dst}  [SKIP — not managed by agentihooks]")
    if managed_servers:
        print(f"  MCP servers from {_CLAUDE_JSON}: {', '.join(sorted(managed_servers.keys()))}")
    else:
        print(f"  MCP servers from {_CLAUDE_JSON}: [none found]")
    print("  agentihooks CLI (uv tool / symlink)")
    print()
    print(f"NOT removed (your data): {STATE_JSON}")
    print()

    if not yes:
        answer = input("Proceed? [y/N] ").strip().lower()
        if answer != "y":
            print("Aborted.")
            sys.exit(0)

    # --- 3. Remove settings.json ---
    if remove_settings:
        settings_path.unlink()
        _cprint(f"[OK] Removed {settings_path}")
    else:
        print(f"[--] Skipped {settings_path} (not managed)")

    # --- 4. Remove symlinks in skills, agents, commands ---
    print()
    for dst_dir, label in [
        (skills_dir, "skill"),
        (agents_dir, "agent"),
        (commands_dir, "command"),
        (rules_dir, "rule"),
    ]:
        n = _remove_agentihooks_symlinks(dst_dir, label)
        if n == 0:
            _cprint(f"  [--] No {label} symlinks found in {dst_dir}")

    # --- 5. Remove agentihooks CLAUDE.md; restore the pre-agentihooks original ---
    # install writes the profile CLAUDE.md into home and appends the manifesto
    # additively, so "removing what agentihooks added" means: restore the file
    # agentihooks originally overwrote (if any), otherwise delete the managed file.
    print()
    if remove_claude_md:
        cmd_state = _load_state()
        original = cmd_state.get("claude_md_original_backup")
        original_path = Path(original) if original else None
        if original_path and original_path.exists():
            # The original predates any third-party block added since (agentibridge
            # et al). Every init carried those forward; dropping them here would
            # destroy live content at the one moment it matters most.
            restored = original_path.read_text()
            if claude_md_dst.exists() and not claude_md_dst.is_symlink():
                restored = _preserve_foreign_blocks(restored, claude_md_dst.read_text())
            if claude_md_dst.is_symlink() or claude_md_dst.exists():
                claude_md_dst.unlink()
            claude_md_dst.write_text(restored)
            _cprint(f"[OK] Restored pre-agentihooks {claude_md_dst} from {original_path.name}")
        else:
            # Nothing to restore, but a neighbour's block must not go with it.
            carried = ""
            if claude_md_dst.exists() and not claude_md_dst.is_symlink():
                carried = _preserve_foreign_blocks("", claude_md_dst.read_text()).strip()
            claude_md_dst.unlink()
            if carried:
                claude_md_dst.write_text(carried + "\n")
                _cprint(f"[OK] Removed agentihooks content from {claude_md_dst}; kept third-party blocks")
            else:
                _cprint(f"[OK] Removed {claude_md_dst} (no pre-agentihooks original recorded)")
        cmd_state.pop("managed_claude_md", None)
        cmd_state.pop("claude_md_original_backup", None)
        _save_state(cmd_state)
    else:
        print(f"[--] Skipped {claude_md_dst} (not managed by agentihooks)")

    # --- 6. Remove MCP servers from ~/.claude.json ---
    print()
    if managed_servers:
        print(f"Removing {len(managed_servers)} MCP server(s) from {_CLAUDE_JSON}:")
        _remove_mcp_from_user_scope(managed_servers)
    else:
        _cprint(f"  [--] No managed MCP servers to remove from {_CLAUDE_JSON}")

    # Clear the managed-MCP ledger — agentihooks manages nothing after uninstall.
    # (state.json itself is preserved below, so the key must be dropped explicitly.)
    ledger_state = _load_state()
    if ledger_state.pop("managed_mcp_servers", None) is not None:
        _save_state(ledger_state)

    # --- 7. Remove bashrc block ---
    if _remove_bashrc_block():
        _cprint(f"[OK] Removed agentihooks block from {_BASHRC}")

    # --- 8. Uninstall CLI ---
    print()
    _uninstall_cli_tool()

    # --- Done ---
    print()
    print("Uninstall complete.")
    print()
    print(f"Note: {AGENTIHOOKS_STATE_DIR}/ was NOT removed (your data).")
    print(f"      .env    : {_ENV_FILE_DST}")
    print(f"      state   : {STATE_JSON}")
    print(f"      Full reset: rm -rf {AGENTIHOOKS_STATE_DIR}")


# ---------------------------------------------------------------------------
# Project install
# ---------------------------------------------------------------------------


def install_project(args: argparse.Namespace) -> None:
    with _sync_lock():
        _install_project_inner(args)


def _install_project_inner(args: argparse.Namespace) -> None:
    project_path = Path(args.path).expanduser().resolve()
    profile_name = args.profile

    if not project_path.exists():
        print(f"ERROR: Project path does not exist: {project_path}", file=sys.stderr)
        sys.exit(1)
    if not project_path.is_dir():
        print(f"ERROR: Project path is not a directory: {project_path}", file=sys.stderr)
        sys.exit(1)

    # Validate profile (built-in or bundle)
    profile_dir = _resolve_profile_dir(profile_name)
    if profile_dir is None:
        available = _available_profiles()
        print(f"ERROR: Profile '{profile_name}' not found.", file=sys.stderr)
        print(f"Available profiles: {', '.join(available)}", file=sys.stderr)
        sys.exit(1)

    rendered_mcp = _build_mcp_config("all")

    mcp_dst = project_path / _MCP_JSON_NAME
    if mcp_dst.exists():
        # Never overwrite — merge new keys only
        try:
            existing_mcp = load_json(mcp_dst)
        except (json.JSONDecodeError, OSError):
            existing_mcp = {}

        existing_servers = existing_mcp.get("mcpServers", {})
        new_servers = rendered_mcp.get("mcpServers", {})
        keys_to_add = {k: v for k, v in new_servers.items() if k not in existing_servers}

        if keys_to_add:
            answer = (
                input(
                    f"{_MCP_JSON_NAME} exists at {mcp_dst}.\n"
                    f"  New servers to add: {', '.join(keys_to_add.keys())}\n"
                    f"  Merge? [y/N] "
                )
                .strip()
                .lower()
            )
            if answer == "y":
                existing_servers.update(keys_to_add)
                existing_mcp["mcpServers"] = existing_servers
                save_json(mcp_dst, existing_mcp)
                _cprint(f"[OK] Merged {len(keys_to_add)} server(s) into {mcp_dst}")
            else:
                _cprint(f"[--] Skipped {mcp_dst} (no changes)")
        else:
            _cprint(f"[--] {mcp_dst} already has all servers — no changes")
    else:
        save_json(mcp_dst, rendered_mcp)
        _cprint(f"[OK] Created {mcp_dst}")

    print()
    print(f"Next: open Claude Code in '{project_path}' and run /mcp to verify.")

    # Register install in state.json
    _register_target_project(project_path, profile_name)
    _snapshot_claude_json()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _scan_mcp_dir(scan_dir: Path) -> list[tuple[Path, dict]]:
    """Scan *scan_dir* for JSON files containing ``mcpServers``.

    Returns a sorted list of (path, servers_dict) tuples.
    """
    results: list[tuple[Path, dict]] = []
    if not scan_dir.is_dir():
        return results
    for f in sorted(scan_dir.glob("*.json")):
        try:
            data = load_json(f)
            if "mcpServers" in data and data["mcpServers"]:
                results.append((f, data["mcpServers"]))
        except (json.JSONDecodeError, OSError):
            pass
    # Also check hidden .json files (e.g., .anton-mcp.json)
    for f in sorted(scan_dir.glob(".*.json")):
        try:
            data = load_json(f)
            if "mcpServers" in data and data["mcpServers"]:
                results.append((f, data["mcpServers"]))
        except (json.JSONDecodeError, OSError):
            pass
    # Deduplicate by path (glob patterns may overlap)
    seen: set[str] = set()
    deduped: list[tuple[Path, dict]] = []
    for fpath, servers in results:
        key = str(fpath)
        if key not in seen:
            seen.add(key)
            deduped.append((fpath, servers))
    return deduped


def _find_companion_env(mcp_file: Path) -> Path | None:
    """Find a companion .env file for an MCP JSON file.

    Checks for: anton-mcp.env, anton.env (from anton-mcp.json).
    """
    stem = mcp_file.stem  # e.g. "anton-mcp"
    for candidate_name in [f"{stem}.env", f"{stem.removesuffix('-mcp')}.env"]:
        candidate = mcp_file.parent / candidate_name
        if candidate.is_file():
            return candidate
    return None


def _display_mcp_list(mcp_files: list[tuple[Path, dict]], tracked: set[str]) -> None:
    """Print a numbered list of MCP files; servers shown as bullet points."""
    for i, (f, servers) in enumerate(mcp_files, 1):
        marker = "  \033[32m[installed]\033[0m" if str(f) in tracked else ""
        print(f"  {i}. {f.name}{marker}")
        for name in servers:
            print(f"     \033[2m•\033[0m {name}")
        env_file = _find_companion_env(f)
        if env_file:
            print(f"     \033[2menv: {env_file.name}\033[0m")


def _prompt_server_selection(servers: dict, action: str = "install") -> dict | None:
    """Stage-2 prompt: pick individual servers or all from *servers*.

    Returns a ``{name: config}`` subset, or ``None`` if the user aborted.
    Accepts:
      0          — all servers
      1          — single server by number
      1,3        — comma-separated selection
    """
    names = list(servers.keys())
    print(f"  0. All ({len(names)} server{'s' if len(names) != 1 else ''})")
    for i, name in enumerate(names, 1):
        print(f"  {i}. {name}")
    print()
    if len(names) == 1:
        try:
            raw = input(f"{action.capitalize()} {names[0]}? [Y/n]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\nAborted.")
            return None
        return None if raw in ("n", "no") else servers
    try:
        raw = input(f"Select (0=all, 1-{len(names)}, or comma list): ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\nAborted.")
        return None
    if not raw or raw.lower() == "q":
        print("Aborted.")
        return None
    if raw == "0":
        return servers
    try:
        selected: dict = {}
        for part in raw.split(","):
            idx = int(part.strip()) - 1
            if not 0 <= idx < len(names):
                raise ValueError(f"{idx + 1}")
            selected[names[idx]] = servers[names[idx]]
        return selected
    except ValueError as exc:
        print(f"Invalid selection: {exc}")
        return None


# ---------------------------------------------------------------------------
# MCP prune helpers
# ---------------------------------------------------------------------------


def _get_valid_mcp_names() -> set[str]:
    """Return the set of MCP server names that actually exist right now.

    Sources of truth:
    1. ~/.claude.json top-level mcpServers (globally registered)
    2. .mcp.json files in registered project directories

    Excludes claude.ai web-session entries entirely.
    """
    if not _CLAUDE_JSON.exists():
        return set()
    data = load_json(_CLAUDE_JSON)
    valid = {name for name in data.get("mcpServers", {}).keys() if not name.startswith("claude.ai ")}
    for proj_path in data.get("projects", {}):
        mcp_file = Path(proj_path) / ".mcp.json"
        if mcp_file.exists():
            try:
                proj_mcp = load_json(mcp_file)
                valid.update(proj_mcp.get("mcpServers", {}).keys())
            except (json.JSONDecodeError, OSError):
                continue
    return valid


def _get_managed_mcp_names() -> set[str]:
    """Return server names that agentihooks sources currently define."""
    try:
        return set(_collect_all_managed_mcp_servers().keys())
    except Exception:
        return set()


def _prune_stale_mcp_servers(known_servers_file: Path, *, verbose: bool = False) -> dict:
    """Remove stale MCP entries from all tracking locations.

    Prunes:
    0. Orphaned mcpServers in ~/.claude.json (managed by agentihooks but
       no longer defined in any source file)
    1. disabledMcpServers in every project entry in ~/.claude.json
    2. known-mcp-servers.json
    3. enabled_mcps in state.json

    Returns summary dict with counts.
    """
    valid = _get_valid_mcp_names()
    summary = {
        "pruned_disabled": 0,
        "pruned_known": 0,
        "projects_touched": 0,
        "pruned_orphaned": 0,
        "pruned_enabled": 0,
    }

    if not valid:
        managed_fallback = _get_managed_mcp_names()
        if managed_fallback:
            valid = managed_fallback
        else:
            return summary

    # 0. Remove orphaned mcpServers from ~/.claude.json
    managed = _get_managed_mcp_names()
    if managed and _CLAUDE_JSON.exists():
        data = load_json(_CLAUDE_JSON)
        current_servers = data.get("mcpServers", {})
        orphaned = {name for name in current_servers if not name.startswith("claude.ai ") and name not in managed}
        if orphaned:
            for name in orphaned:
                del current_servers[name]
            save_json(_CLAUDE_JSON, data)
            summary["pruned_orphaned"] = len(orphaned)
            valid -= orphaned
            if verbose:
                for name in sorted(orphaned):
                    print(f"  Removed orphaned server: {name}")

    # 1. Prune disabledMcpServers in ~/.claude.json projects
    if _CLAUDE_JSON.exists():
        data = load_json(_CLAUDE_JSON)
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
                    print(f"  Pruned {removed_count} stale entries from {proj_path}: {sorted(stale)}")
        if claude_json_dirty:
            save_json(_CLAUDE_JSON, data)
    else:
        projects = {}

    # 2. Prune known-mcp-servers.json
    if known_servers_file.exists():
        known_data = load_json(known_servers_file)
        known_list = set(known_data.get("knownMcpServers", []))
        stale_known = known_list - valid
        if stale_known:
            known_data["knownMcpServers"] = sorted(known_list - stale_known)
            save_json(known_servers_file, known_data)
            summary["pruned_known"] = len(stale_known)
            if verbose:
                print(f"  Pruned {len(stale_known)} from known-mcp-servers.json: {sorted(stale_known)}")

    # 3. Prune enabled_mcps in state.json
    state = _load_state()
    state_projects = state.get("targets", {}).get("projects", {})
    state_dirty = False
    for proj_path, proj_info in state_projects.items():
        enabled = proj_info.get("enabled_mcps", [])
        if not enabled:
            continue
        cleaned = [s for s in enabled if s in valid]
        removed = len(enabled) - len(cleaned)
        if removed > 0:
            proj_info["enabled_mcps"] = cleaned
            summary["pruned_enabled"] += removed
            state_dirty = True
    if state_dirty:
        _save_state(state)

    return summary


def cmd_mcp_action(action: str, scan_dir: Path | None = None, *, mcp_path: Path | None = None) -> None:
    """Handle ``agentihooks mcp list|install|uninstall|sync|add``.

    *scan_dir* defaults to ``~/.agentihooks/``.
    """
    if scan_dir is None:
        scan_dir = AGENTIHOOKS_STATE_DIR

    tracked = set(_load_state().get("mcpFiles", []))

    if action == "list":
        mcp_files = _scan_mcp_dir(scan_dir)
        if not mcp_files:
            print(f"No MCP files found in {scan_dir}")
            print(f"\nDrop .json files with a mcpServers key into {scan_dir}")
            return
        print(f"MCP files in {scan_dir}:\n")
        _display_mcp_list(mcp_files, tracked)
        if tracked:
            print(f"\nCurrently installed: {len(tracked)} file(s)")
        print("\nTo install:   agentihooks mcp install")
        print("To uninstall: agentihooks mcp uninstall")

    elif action == "install":
        mcp_files = _scan_mcp_dir(scan_dir)
        if not mcp_files:
            print(f"No MCP files found in {scan_dir}")
            print(f"\nDrop .json files with a mcpServers key into {scan_dir}")
            return
        # Stage 1 — pick a file
        if len(mcp_files) == 1:
            selected_file, all_servers = mcp_files[0]
            print(f"{selected_file.name}\n")
            for name in all_servers:
                print(f"  \033[2m•\033[0m {name}")
        else:
            print(f"MCP files in {scan_dir}:\n")
            _display_mcp_list(mcp_files, tracked)
            print()
            try:
                raw = input(f"Enter file number (1-{len(mcp_files)}, or q to quit): ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\nAborted.")
                sys.exit(0)
            if raw.lower() == "q":
                print("Aborted.")
                return
            try:
                idx = int(raw) - 1
                if not 0 <= idx < len(mcp_files):
                    raise ValueError
            except ValueError:
                print("Invalid selection.")
                sys.exit(1)
            selected_file, all_servers = mcp_files[idx]
        # Stage 2 — pick server(s) from the file
        print(f"\nServers in {selected_file.name}:\n")
        selected_servers = _prompt_server_selection(all_servers)
        if selected_servers is None:
            return
        print()
        _merge_mcp_to_user_scope(selected_servers)
        _state_add_mcp(selected_file)
        print("  Restart Claude Code for the changes to take effect.")

    elif action == "uninstall":
        if not tracked:
            print("No MCP files currently installed — nothing to uninstall.")
            return
        # Build list of installed files with their server dicts
        installed: list[tuple[Path, dict]] = []
        for path_str in sorted(tracked):
            p = Path(path_str)
            if not p.exists():
                installed.append((p, {}))
                continue
            try:
                data = load_json(p)
                installed.append((p, data.get("mcpServers", {})))
            except (json.JSONDecodeError, OSError):
                installed.append((p, {}))
        # Stage 1 — pick a file
        if len(installed) == 1:
            selected_file, all_servers = installed[0]
            print(f"{selected_file.name}\n")
            for name in all_servers:
                print(f"  \033[2m•\033[0m {name}")
        else:
            print("Installed MCP files:\n")
            _display_mcp_list(installed, tracked)
            print()
            try:
                raw = input(f"Enter file number (1-{len(installed)}, or q to quit): ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\nAborted.")
                sys.exit(0)
            if raw.lower() == "q":
                print("Aborted.")
                return
            try:
                idx = int(raw) - 1
                if not 0 <= idx < len(installed):
                    raise ValueError
            except ValueError:
                print("Invalid selection.")
                sys.exit(1)
            selected_file, all_servers = installed[idx]
        if not all_servers:
            _cprint(f"  [!!] Cannot read servers from {selected_file} — removing from tracking.")
            _state_remove_mcp(selected_file)
            return
        # Stage 2 — pick server(s) to remove
        print(f"\nServers in {selected_file.name}:\n")
        selected_servers = _prompt_server_selection(all_servers, action="uninstall")
        if selected_servers is None:
            return
        print()
        _remove_mcp_from_user_scope(selected_servers)
        # Remove file from state only if all its servers were uninstalled
        if set(selected_servers.keys()) >= set(all_servers.keys()):
            _state_remove_mcp(selected_file)
        print("  Restart Claude Code for the changes to take effect.")

    elif action == "sync":
        sync_user_mcp()

    elif action == "add":
        if mcp_path is None:
            print("Usage: agentihooks mcp add <path>")
            sys.exit(1)
        manage_user_mcp(mcp_path)
        print("\nRestart Claude Code for the changes to take effect.")

    else:
        print(f"Unknown action: {action}")
        print("Usage: agentihooks mcp [list|install|uninstall|sync|add]")
        sys.exit(1)


def cmd_ignore(target_dir: Path, *, force: bool = False) -> None:
    """Create a .claudeignore in *target_dir* (defaults to cwd).

    If the file already exists it is left untouched unless --force is passed,
    in which case it is overwritten with a fresh copy of the template.
    """
    dest = target_dir / ".claudeignore"

    if dest.exists() and not force:
        _cprint(f"  [--] {dest} already exists — use --force to overwrite")
        return

    action = "Overwrote" if dest.exists() else "Created"
    dest.write_text(CLAUDEIGNORE_TEMPLATE, encoding="utf-8")
    _cprint(f"  [OK] {action} {dest}")
    print("       Edit it to add project-specific exclusions.")


def cmd_claude(extra_args: list[str]) -> None:
    """Launch claude with bypassPermissions. No other flags injected."""
    cmd = ["claude", "--dangerously-skip-permissions", *extra_args]

    # Source env first, then exec claude
    env_file = _ENV_FILE_DST
    if env_file.is_file():
        from dotenv import dotenv_values

        for k, v in dotenv_values(env_file).items():
            if v is not None:
                os.environ[k] = v
        # Also load companion .env files
        for f in sorted(env_file.parent.glob("*.env")):
            if f != env_file:
                for k, v in dotenv_values(f).items():
                    if v is not None:
                        os.environ[k] = v

    os.execvp("claude", cmd)


# ---------------------------------------------------------------------------
# migrate — fix ~/.claude.json project entries after repos move
# ---------------------------------------------------------------------------


_BROADCAST_EMIT_SYSTEM_PROMPT = """\
You are a broadcast CLI agent. You MUST run an agentihooks broadcast command. \
NEVER respond conversationally. NEVER skip the command. ALWAYS execute a Bash tool call.

RULE: The user's ENTIRE input is ALWAYS a broadcast to send, unless it \
explicitly says "clear", "delete", "remove", "list", or "show". \
When in doubt, CREATE the broadcast. Do not interpret the message — just send it.

ACTIONS:
1. DEFAULT — CREATE a broadcast (this is what you do 95% of the time):
   Parse for severity hints: "critical"/"urgent"/"emergency"/"stop" → -s critical; \
"alert"/"warning"/"attention"/"heads up" → -s alert; otherwise omit -s (defaults to info).
   Parse for TTL: "2 hours" → -t 2h, "30 min" → -t 30m, "1 day" → -t 1d. Omit if absent.
   Command: agentihooks broadcast "the message" [-s severity] [-t ttl]

2. CLEAR (only if user explicitly says clear/delete/remove/cancel):
   agentihooks broadcast --clear
   agentihooks broadcast --clear <id>

3. LIST (only if user explicitly says list/show/check):
   agentihooks broadcast --list

Examples:
- "deploy freeze for 2 hours, critical"
  → agentihooks broadcast "deploy freeze for 2 hours" -s critical -t 2h
- "this is a quick message, acknowledge it"
  → agentihooks broadcast "this is a quick message, acknowledge it"
- "sonarqube is down on anton"
  → agentihooks broadcast "sonarqube is down on anton"
- "clear all broadcasts" → agentihooks broadcast --clear
- "what broadcasts are active" → agentihooks broadcast --list

You MUST call the Bash tool. Text-only responses are FORBIDDEN.\
"""


def _broadcast_emit(natural_input: str) -> None:
    """Use Claude Haiku to parse natural language into a broadcast command."""
    import shutil
    import subprocess as sp

    claude_bin = shutil.which("claude")
    if not claude_bin:
        print("Error: 'claude' CLI not found in PATH.", file=sys.stderr)
        sys.exit(1)

    if not natural_input.strip():
        print('Error: emit requires a message. Usage: agentihooks broadcast emit "your message"', file=sys.stderr)
        sys.exit(1)

    cmd = [
        claude_bin,
        "-p",
        "--model",
        "haiku",
        "--permission-mode",
        "bypassPermissions",
        "--no-session-persistence",
        "--system-prompt",
        _BROADCAST_EMIT_SYSTEM_PROMPT,
        "--tools",
        "Bash",
        "--allowedTools",
        "Bash(agentihooks*)",
        "--disallowedTools",
        "Read,Write,Edit,Glob,Grep,WebSearch,WebFetch,Agent",
    ]

    print(f'Interpreting: "{natural_input}"')
    result = sp.run(cmd, input=natural_input, stdout=sp.PIPE, stderr=sp.STDOUT, text=True, timeout=60)

    output = result.stdout.strip() if result.stdout else ""
    if result.returncode != 0:
        print(f"Error: claude exited with code {result.returncode}", file=sys.stderr)
        if output:
            print(output, file=sys.stderr)
        sys.exit(1)

    if output:
        print(output)
    else:
        print("Broadcast emitted (no output from claude).")


def _parse_ttl_string(ttl_raw: str | None) -> int:
    """Parse a TTL string like '5m', '1h', '30' into seconds. Returns 0 if None."""
    if not ttl_raw:
        return 0
    _ttl_map = {"m": 60, "h": 3600, "d": 86400}
    if ttl_raw[-1] in _ttl_map and ttl_raw[:-1].isdigit():
        return int(ttl_raw[:-1]) * _ttl_map[ttl_raw[-1]]
    if ttl_raw.isdigit():
        return int(ttl_raw)
    print(f"Error: invalid TTL '{ttl_raw}'. Use: 5m, 30m, 1h, 8h, 24h, or seconds.", file=sys.stderr)
    sys.exit(1)


def _cmd_channel(args: argparse.Namespace) -> None:
    """Handle the channel CLI command."""
    sys.path.insert(0, str(AGENTIHOOKS_ROOT))
    action = args.action

    if action == "list":
        from hooks.context.broadcast import list_broadcasts

        msgs = list_broadcasts()
        channels: dict[str, int] = {}
        global_count = 0
        for m in msgs:
            ch = m.get("channel")
            if ch:
                channels[ch] = channels.get(ch, 0) + 1
            else:
                global_count += 1
        if not channels and global_count == 0:
            print("No active messages.")
            return
        if global_count:
            print(f"  global: {global_count} message(s)")
        for ch, count in sorted(channels.items()):
            print(f"  {ch}: {count} message(s)")

    elif action == "publish":
        if not args.channel_name:
            print("Error: channel name required.", file=sys.stderr)
            sys.exit(1)
        msg_text = " ".join(args.chan_message) if args.chan_message else ""
        if not msg_text:
            print("Error: message required.", file=sys.stderr)
            sys.exit(1)
        from hooks.context.broadcast import create_broadcast

        ttl = _parse_ttl_string(getattr(args, "ttl", None))
        msg_id = create_broadcast(
            message=msg_text,
            severity=args.severity,
            ttl_seconds=ttl,
            source="cli-channel",
            channel=args.channel_name,
        )
        if msg_id:
            print(f"{_GREEN}[OK]{_RESET} Published to '{args.channel_name}' (id: {msg_id})")
        else:
            print("Error: empty message.", file=sys.stderr)
            sys.exit(1)


def _cmd_brain(args: argparse.Namespace) -> None:
    """Handle the brain CLI command."""
    sys.path.insert(0, str(AGENTIHOOKS_ROOT))
    from hooks.context.brain_adapter import force_refresh, get_status

    if args.action == "status":
        status = get_status()
        print(f"  enabled:    {status.get('enabled')}")
        print(f"  source:     {status.get('source_type')} → {status.get('source_path')}")
        print(f"  channel:    {status.get('channel')}")
        print(f"  entries:    {status.get('entry_count')}")
        print(f"  hash:       {status.get('content_hash') or '(none)'}")
        print(f"  refresh:    every {status.get('refresh_interval')} turns")

    elif args.action == "refresh":
        published = force_refresh()
        if published:
            print(f"{_GREEN}[OK]{_RESET} Brain content refreshed and published.")
        else:
            print("No changes (content unchanged or source empty).")


def _detect_active_profile() -> str:
    """Find the active profile from ~/.claude/CLAUDE.md symlink target."""
    claude_md = Path.home() / ".claude" / "CLAUDE.md"
    try:
        if claude_md.is_symlink():
            target = os.readlink(str(claude_md))
            # Extract profile name from path like ".../profiles/anton/.claude/CLAUDE.md"
            parts = Path(target).parts
            if "profiles" in parts:
                idx = parts.index("profiles")
                if idx + 1 < len(parts):
                    return parts[idx + 1]
    except OSError:
        pass
    return "anton"  # fallback default


def _cmd_refresh_rules(args: argparse.Namespace) -> None:
    """Handle the refresh-rules CLI command — push rules to running sessions."""
    sys.path.insert(0, str(AGENTIHOOKS_ROOT))
    from hooks.context.rules_refresh import (
        _delete_marker,
        _load_marker,
        collect_profile_rules,
        write_refresh_marker,
    )

    profile = args.profile or _detect_active_profile()
    rules_dir = Path.home() / ".claude" / "rules"
    claude_md = Path.home() / ".claude" / "CLAUDE.md"
    claude_local_md = Path.home() / ".claude" / "CLAUDE.local.md"

    if args.clear:
        existing = _load_marker(profile)
        if existing:
            _delete_marker(profile)
            print(
                f"{_GREEN}[OK]{_RESET} Cleared pending marker for profile '{profile}' "
                f"(was targeting {len(existing.get('pending', []))} sessions)."
            )
        else:
            print(f"No pending marker for profile '{profile}'.")
        return

    if not rules_dir.exists() and not claude_md.exists() and not claude_local_md.exists():
        print(f"[ERROR] No rules found at {rules_dir} / {claude_md} / {claude_local_md}. Is agentihooks installed?")
        sys.exit(1)

    payload = collect_profile_rules(rules_dir, claude_md, claude_local_md)

    if args.dry_run:
        import hashlib as _hash

        content_hash = _hash.sha256(payload.encode("utf-8")).hexdigest()[:16]
        from hooks.context.rules_refresh import _collect_pending_sessions

        pending = _collect_pending_sessions()
        print("=== DRY RUN — no marker written ===")
        print(f"Profile:        {profile}")
        print(f"Content hash:   {content_hash}")
        print(f"Payload size:   {len(payload):,} chars")
        print(f"Alive sessions: {len(pending)}")
        for sid in pending:
            print(f"  - {sid}")
        print("=====================================")
        return

    result = write_refresh_marker(profile, payload)
    marker_path = result["marker_path"]
    pending_count = result["pending_count"]
    content_hash = result["content_hash"]

    print(f"{_GREEN}[OK]{_RESET} Refresh marker written for profile '{profile}'.")
    print(f"  Marker:         {marker_path}")
    print(f"  Content hash:   {content_hash}")
    print(f"  Alive sessions: {pending_count}")
    if pending_count == 0:
        print("  No running sessions to notify — marker will GC in 24h.")
    else:
        print("  Each session will consume the refresh on its next UserPromptSubmit.")


def _cmd_broadcast(args: argparse.Namespace) -> None:
    """Handle the broadcast CLI command."""
    sys.path.insert(0, str(AGENTIHOOKS_ROOT))
    from hooks.context.broadcast import clear_broadcasts, create_broadcast, list_broadcasts

    if getattr(args, "bcast_list", False):
        msgs = list_broadcasts()
        if not msgs:
            print("No active broadcasts.")
            return
        for m in msgs:
            sev = m.get("severity", "?").upper()
            mid = m.get("id", "?")
            msg = m.get("message", "")
            exp = m.get("expires_at", "")
            pers = " [persistent]" if m.get("persistent") else ""
            print(f"  [{sev}] {mid} — {msg}{pers}  (expires: {exp})")
        return

    bcast_clear = getattr(args, "bcast_clear", None)
    if bcast_clear is not None:
        if bcast_clear == "__ALL__":
            clear_broadcasts()
            print("All broadcasts cleared.")
        else:
            clear_broadcasts(message_id=bcast_clear)
            print(f"Broadcast {bcast_clear} cleared.")
        return

    # Join nargs="*" words into a single message
    words = getattr(args, "message", None) or []
    if not words:
        print("Error: message is required (or use --list / --clear)", file=sys.stderr)
        sys.exit(1)

    # AI-assisted emit mode
    if words[0] == "emit":
        natural_input = " ".join(words[1:])
        _broadcast_emit(natural_input)
        return

    message = " ".join(words)

    # Parse TTL string
    ttl_seconds = 0
    ttl_raw = getattr(args, "ttl", None)
    if ttl_raw:
        _ttl_map = {"m": 60, "h": 3600, "d": 86400}
        if ttl_raw[-1] in _ttl_map and ttl_raw[:-1].isdigit():
            ttl_seconds = int(ttl_raw[:-1]) * _ttl_map[ttl_raw[-1]]
        elif ttl_raw.isdigit():
            ttl_seconds = int(ttl_raw)
        else:
            print(f"Error: invalid TTL '{ttl_raw}'. Use: 5m, 30m, 1h, 8h, 24h, or seconds.", file=sys.stderr)
            sys.exit(1)

    msg_id = create_broadcast(
        message,
        severity=args.severity,
        ttl_seconds=ttl_seconds,
        source=args.source,
        persistent=args.persistent or None,
    )
    if msg_id:
        print(f"Broadcast created: {msg_id} [{args.severity}]")
    else:
        print("Error: failed to create broadcast.", file=sys.stderr)
        sys.exit(1)


def _cmd_enforcement(args: argparse.Namespace) -> None:
    """Handle the enforcement CLI command."""
    sys.path.insert(0, str(AGENTIHOOKS_ROOT))
    from hooks.context.enforcement import (
        add_enforcement,
        clear_enforcement,
        list_enforcements,
    )

    action = getattr(args, "action", None)

    if action == "list":
        entries = list_enforcements()
        if not entries:
            print("No active enforcements.")
            return
        print(f"{'SOURCE':<10} {'ID':<12} {'CADENCE':<8} {'TAG':<16} MESSAGE")
        for e in entries:
            src = e.get("source", "runtime")
            eid = e.get("id", "?")
            cad = e.get("cadence", "?")
            tag = e.get("tag", "") or "-"
            msg = e.get("message", "")
            print(f"[{src}]".ljust(10) + f"{eid:<12} {cad:<8} {tag:<16} {msg}")
        return

    if action == "clear":
        enf_id = getattr(args, "enf_id", "") or ""
        tag = getattr(args, "tag", "") or ""
        if enf_id:
            count = clear_enforcement(enforcement_id=enf_id)
            print(f"Cleared {count} enforcement(s) matching id={enf_id}.")
        elif tag:
            count = clear_enforcement(tag=tag)
            print(f"Cleared {count} enforcement(s) matching tag={tag}.")
        else:
            count = clear_enforcement()
            print(f"Cleared all {count} enforcement(s).")
        return

    if action == "set":
        words = getattr(args, "enf_args", None) or []
        if not words:
            print(
                'Error: enforcement set requires a message. Example: agentihooks enforcement set "patches forbidden"',
                file=sys.stderr,
            )
            sys.exit(1)
        # Last token is cadence ONLY if it's a positive integer; otherwise
        # treat the whole input as the message and use default cadence.
        cadence = 5
        if len(words) >= 2 and words[-1].isdigit() and int(words[-1]) >= 1:
            cadence = int(words[-1])
            message = " ".join(words[:-1]).strip()
        else:
            message = " ".join(words).strip()
        if not message:
            print("Error: message is required.", file=sys.stderr)
            sys.exit(1)
        tag = getattr(args, "tag", "") or None
        enf_id = add_enforcement(message=message, cadence=cadence, tag=tag)
        if enf_id:
            print(f"Enforcement created: {enf_id} (every {cadence} tool calls)")
        else:
            print("Error: failed to create enforcement.", file=sys.stderr)
            sys.exit(1)
        return

    print("Error: unknown enforcement action.", file=sys.stderr)
    sys.exit(1)


def cmd_migrate(args) -> None:
    """Remap ~/.claude.json project entries when repos move to a new parent dir."""
    target = Path(args.target_path).expanduser().resolve()
    dry_run = getattr(args, "dry_run", False)

    if not target.exists():
        print(f"Error: {target} does not exist", file=sys.stderr)
        sys.exit(1)

    # Determine repos to migrate
    if (target / ".git").exists():
        repos = [target]
    else:
        repos = sorted(p for p in target.iterdir() if p.is_dir() and (p / ".git").exists())
        if not repos:
            print(f"Error: no git repos found under {target}", file=sys.stderr)
            sys.exit(1)

    # Load ~/.claude.json
    if not _CLAUDE_JSON.exists():
        print("Error: ~/.claude.json not found", file=sys.stderr)
        sys.exit(1)
    try:
        data = load_json(_CLAUDE_JSON)
    except (json.JSONDecodeError, OSError) as e:
        print(f"Error reading ~/.claude.json: {e}", file=sys.stderr)
        sys.exit(1)

    projects = data.get("projects", {})
    if not projects:
        print("No project entries in ~/.claude.json — nothing to migrate.")
        return

    # Build remap table: new_path → old_path
    remap: list[tuple[str, str]] = []  # (old, new)
    skipped: list[tuple[str, str]] = []  # (repo_name, reason)

    for repo in repos:
        new_path = str(repo)
        basename = repo.name

        # Find old entries matching by basename (different path, same repo name)
        candidates = [p for p in projects if Path(p).name == basename and p != new_path]

        if not candidates:
            skipped.append((basename, "no old entry found"))
            continue

        # Filter: skip if old path still exists on disk (different repo, not a move)
        moved = [c for c in candidates if not Path(c).exists()]
        if not moved:
            skipped.append((basename, f"old path(s) still exist on disk: {candidates}"))
            continue

        # Remap each old entry → new path (merge into existing new entry if present)
        for old_path in moved:
            remap.append((old_path, new_path))

    if not remap:
        print("Nothing to migrate.")
        if skipped:
            print("\nSkipped:")
            for name, reason in skipped:
                print(f"  {name}: {reason}")
        return

    # Print remap table
    print(f"\n{'OLD PATH':<60} → NEW PATH")
    print("─" * 120)
    for old, new in remap:
        print(f"  {old:<58} → {new}")
    print()

    if dry_run:
        print("[DRY RUN] No changes written.")
        return

    # Apply remaps
    for old, new in remap:
        old_data = projects.pop(old, {})
        new_data = projects.get(new, {})
        # Merge: old data is base, new data (if any) overrides
        merged = {**old_data, **new_data}
        projects[new] = merged

    state = _load_state()
    active_profile = state.get("targets", {}).get("global", {}).get("profile", "default")

    # Save
    data["projects"] = projects
    save_json(_CLAUDE_JSON, data)
    print(f"[OK] Migrated {len(remap)} project(s) in ~/.claude.json")

    # Register targets in state.json
    for _old, new in remap:
        _register_target_project(Path(new), active_profile)
    print(f"[OK] Registered {len(remap)} target(s) in state.json")

    _snapshot_claude_json()
    print("[OK] Updated claude_json_snapshot in state.json")

    if skipped:
        print("\nSkipped:")
        for name, reason in skipped:
            print(f"  {name}: {reason}")


def main() -> None:
    _argv = sys.argv[1:]

    # Fast path: "agentihooks claude ..." bypasses argparse entirely
    # so that any claude flags (-r, --resume, -p, etc.) pass through untouched
    if _argv and _argv[0] == "claude":
        cmd_claude(_argv[1:])
        return

    parser = argparse.ArgumentParser(
        description="agentihooks — Claude Code harness: hooks, profiles, skills, MCPs.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=_USAGE_TEXT,
    )
    parser.add_argument(
        "--list-profiles",
        action="store_true",
        help="List available profiles and exit",
    )
    parser.add_argument(
        "--query",
        action="store_true",
        help="Print the currently active global profile name and exit",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"agentihooks {_get_version()}",
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("version", help="Print version")
    update_p = sub.add_parser("update", help="Self-update agentihooks")
    update_p.add_argument("--source", default="", help="Custom pip source (default: editable reinstall)")

    unsub = sub.add_parser("uninstall", help="Remove all agentihooks artifacts from the system")
    unsub.add_argument("--yes", "-y", action="store_true", help="Skip confirmation prompt")

    init_p = sub.add_parser("init", help="Initialize agentihooks (global setup from bundle)")
    init_p.add_argument(
        "--bundle", default=None, help="Path to bundle directory (first-time setup: link bundle + global install)"
    )
    init_p.add_argument("--profile", dest="init_profile", default=None, help="Profile to use (headless mode)")
    init_p.add_argument(
        "--force",
        action="store_true",
        default=False,
        help="Clean install — wipe ~/.agentihooks/ (except .env) and re-init from scratch",
    )
    init_p.add_argument(
        "--settings-profile",
        dest="init_settings_profile",
        default=None,
        help="Settings-only overlay profile (applies settings.json/MCP on top, keeps persona from --profile)",
    )
    init_p.add_argument(
        "--dry-run",
        action="store_true",
        help="Not implemented for init — refuses rather than performing a real install",
    )
    init_p.add_argument(
        "--link-profile",
        action="append",
        default=[],
        metavar="NAME=PATH",
        help="Register an external profile directory and append it to the chain. Repeatable (NAME=PATH).",
    )
    init_p.add_argument(
        "--no-discover",
        action="store_true",
        default=False,
        help="Skip bundle auto-discovery hint when state has no bundle linked.",
    )

    bundle_p = sub.add_parser("bundle", help="Manage the linked bundle (link, unlink, list, pull)")
    bundle_p.add_argument(
        "action",
        choices=["link", "unlink", "list", "pull"],
        help="link <path> | unlink | list | pull",
    )
    bundle_p.add_argument("bundle_path", nargs="?", default=None, help="Bundle directory path (for link)")
    bundle_p.add_argument("--rebase", action="store_true", help="Use --rebase when pulling")

    lp_p = sub.add_parser(
        "link-profile",
        help="Link an external profile directory and append it to the chain (link | unlink | list)",
    )
    lp_p.add_argument(
        "action",
        choices=["link", "unlink", "list"],
        help="link <path> | unlink <name> | list",
    )
    lp_p.add_argument("target", nargs="?", default=None, help="Path to link, or name to unlink")
    lp_p.add_argument("--name", default=None, help="Alias for the linked profile (defaults to dir basename)")
    lp_p.add_argument(
        "--no-append",
        action="store_true",
        help="Register the path but do not modify the active chain",
    )
    lp_p.add_argument(
        "--no-init",
        action="store_true",
        help="Skip the immediate re-install (operator runs 'agentihooks init' later)",
    )

    sub.add_parser("claude", help="Launch claude with --dangerously-skip-permissions (no other flags injected)")

    ign_p = sub.add_parser("ignore", help="Create a .claudeignore in the current directory")
    ign_p.add_argument(
        "path",
        nargs="?",
        default=".",
        help="Target directory (default: current directory)",
    )
    ign_p.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing .claudeignore",
    )

    # ── Token optimization CLI tools ──────────────────────────────────
    lint_p = sub.add_parser("lint-claude", help="Analyze CLAUDE.md token cost and suggest skill extraction")
    lint_p.add_argument("lint_path", nargs="?", default=None, help="Path to CLAUDE.md (default: ~/.claude/CLAUDE.md)")

    extract_p = sub.add_parser("extract-skill", help="Extract a CLAUDE.md section into a skill")
    extract_p.add_argument("section", help='Section heading to extract (e.g. "Commands")')
    extract_p.add_argument("--name", required=True, help="Skill name for the output directory")
    extract_p.add_argument("--source", default=None, help="Path to CLAUDE.md (default: ~/.claude/CLAUDE.md)")
    extract_p.add_argument("--output-dir", default=None, help="Output directory (default: source's .claude/commands/)")

    mcp_p = sub.add_parser("mcp", help="MCP surface area analysis")
    mcp_p.add_argument("mcp_action", choices=["report"], help="Action to perform")
    mcp_p.add_argument("--project", default=None, help="Project path to include (default: CWD)")

    sub.add_parser("status", help="Show installation health, cost guardrails, and system state")

    doctor_p = sub.add_parser(
        "doctor",
        help="Diagnose hook health: simulate every event, validate stdout JSON, surface broken hooks",
    )
    doctor_p.add_argument(
        "--debug-hook",
        action="store_true",
        help="Run each hook event with synthetic payload and assert protocol invariants",
    )
    doctor_p.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of CLI report",
    )

    prune_p = sub.add_parser("prune", help="Remove stale MCP entries from all config files")
    prune_p.add_argument("--verbose", "-v", action="store_true", help="Show details of each pruned entry")

    sp_p = sub.add_parser(
        "settings-profile",
        help="Quick-switch settings layer only (keeps persona/rules/CLAUDE.md intact)",
    )
    sp_p.add_argument("sp_name", nargs="?", default=None, help="Settings profile name (or --clear to remove overlay)")
    sp_p.add_argument(
        "--clear", action="store_true", help="Remove settings overlay, revert to persona profile defaults"
    )

    p_migrate = sub.add_parser("migrate", help="Fix ~/.claude.json project entries after repos move")
    p_migrate.add_argument("target_path", type=str, help="New repo path or parent dir containing repos")
    p_migrate.add_argument("--dry-run", action="store_true", help="Show what would change without writing")

    bcast_p = sub.add_parser(
        "broadcast",
        help="Send a message to all active Claude Code sessions",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
severity levels:
  info      delivered once per session (default, TTL 4h)
  alert     delivered every turn, persistent (TTL 1h)
  critical  delivered every turn + every tool call, persistent (TTL 30m)

examples:
  agentihooks broadcast "SonarQube is down"
  agentihooks broadcast "Deploy freeze until 3am" -s alert
  agentihooks broadcast "STOP ALL WRITES" -s critical -t 15m
  agentihooks broadcast --list
  agentihooks broadcast --clear
  agentihooks broadcast --clear abc123  # clear specific message

ai-assisted (emit):
  agentihooks broadcast emit "deploy freeze for the next 2 hours, critical"
  agentihooks broadcast emit "sonarqube is down, let sessions know"
  agentihooks broadcast emit "stop all writes to postgres for 30 min, urgent"
  Uses Claude Haiku to parse natural language into severity, TTL, and message.
""",
    )
    bcast_p.add_argument(
        "message", nargs="*", default=None, help="Message to broadcast (prefix with 'emit' for AI-assisted)"
    )
    bcast_p.add_argument(
        "-s",
        "--severity",
        default="info",
        choices=["info", "alert", "critical"],
        help="Message severity (default: info)",
    )
    bcast_p.add_argument("-t", "--ttl", default=None, help="TTL: 5m, 30m, 1h, 8h, 24h, or seconds")
    bcast_p.add_argument("--persistent", action="store_true", default=False, help="Force persistent delivery")
    bcast_p.add_argument("--source", default="operator", help="Message source label")
    bcast_p.add_argument("--list", action="store_true", dest="bcast_list", help="List active broadcasts")
    bcast_p.add_argument(
        "--clear",
        nargs="?",
        const="__ALL__",
        default=None,
        dest="bcast_clear",
        help="Clear all broadcasts, or a specific ID",
    )

    # --- Channel subcommand ---
    chan_p = sub.add_parser(
        "channel",
        help="Manage broadcast channels (targeted messaging)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
examples:
  agentihooks channel list
  agentihooks channel publish brain "Hot arcs updated" -s info -t 1h
""",
    )
    chan_p.add_argument("action", choices=["list", "publish"], help="Channel action")
    chan_p.add_argument("channel_name", nargs="?", default=None, help="Channel name")
    chan_p.add_argument("chan_message", nargs="*", default=None, help="Message (for publish)")
    chan_p.add_argument("-s", "--severity", default="info", choices=["info", "alert", "critical"])
    chan_p.add_argument("-t", "--ttl", default=None, help="TTL: 5m, 30m, 1h, 8h, 24h, or seconds")

    # --- Brain subcommand ---
    brain_p = sub.add_parser("brain", help="Manage the brain adapter (knowledge injection)")
    brain_p.add_argument("action", choices=["status", "refresh"], help="Brain action")

    # --- Enforcement subcommand ---
    enf_p = sub.add_parser(
        "enforcement",
        help="Manage drumbeat enforcement reminders (re-inject every N tool calls)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
cadence:
  How often the message re-injects into the agent's context.
  Cadence = N means "every Nth tool call, the agent sees this reminder again."
  Lower N = more drumbeat (cadence 3 = harder enforcement).
  Higher N = quieter nudge (cadence 15 = soft style cue).
  Default: 5.

examples:
  agentihooks enforcement set "patches forbidden — code only"      # default cadence (every 5 tool calls)
  agentihooks enforcement set "use Monitor not CronCreate" 10      # custom cadence
  agentihooks enforcement list
  agentihooks enforcement clear                                     # remove ALL
  agentihooks enforcement clear --id abc12345                       # remove one
""",
    )
    enf_p.add_argument("action", choices=["set", "list", "clear"], help="Enforcement action")
    enf_p.add_argument(
        "enf_args",
        nargs="*",
        default=None,
        help="set: <message> [cadence]. Cadence = re-inject every N tool calls (default 5)",
    )
    enf_p.add_argument("--tag", default="", help="Optional grouping tag")
    enf_p.add_argument("--id", dest="enf_id", default="", help="Clear by enforcement id")

    # --- Rules refresh subcommand ---
    rr_p = sub.add_parser(
        "refresh-rules",
        help="Push profile rule updates into all running Claude sessions (one-shot)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
examples:
  agentihooks refresh-rules                    # detect active profile, refresh all alive sessions
  agentihooks refresh-rules --profile anton    # explicit profile
  agentihooks refresh-rules --dry-run          # show what would be pushed without writing marker
  agentihooks refresh-rules --clear            # delete any existing marker (cancel a pending push)

notes:
  - Each running session consumes the refresh ONCE on its next UserPromptSubmit
  - Sessions that start AFTER the push never see the marker (they get fresh rules at SessionStart)
  - Marker auto-expires after 24h
""",
    )
    rr_p.add_argument("--profile", default=None, help="Profile name (default: active profile)")
    rr_p.add_argument("--dry-run", action="store_true", help="Print what would be pushed without writing")
    rr_p.add_argument("--clear", action="store_true", help="Delete existing marker instead of pushing")

    # --- Sessions subcommand ---
    sess_p = sub.add_parser(
        "sessions",
        help="List and reopen recent Claude Code sessions (24h crash-recovery registry)",
    )
    sess_sub = sess_p.add_subparsers(dest="sessions_action")
    sess_list_p = sess_sub.add_parser(
        "list", aliases=["ls"], help="List the most recent sessions (default: last 10 in 24h window)"
    )
    sess_list_p.add_argument("--hours", type=int, default=24, help="Lookback window (default: 24)")
    sess_list_p.add_argument("--limit", type=int, default=10, help="How many to show (default: 10, 0 = all)")
    sess_reopen_p = sess_sub.add_parser(
        "reopen",
        aliases=["open"],
        help="Reopen specific sessions by IDX from `sessions list` (e.g. `sessions reopen 6 7 8`)",
    )
    sess_reopen_p.add_argument(
        "indices",
        nargs="+",
        help="IDX numbers from `sessions list` — space or comma-separated (e.g. `6 7` or `6,7,8`)",
    )
    sess_reopen_p.add_argument(
        "--force",
        action="store_true",
        help="Override busy-JSONL and alive-status guards (DANGEROUS — can fork duplicate sessions)",
    )
    sess_backfill_p = sess_sub.add_parser(
        "backfill",
        help="Seed the registry from ~/.claude/projects/*.jsonl (for pre-existing sessions)",
    )
    sess_backfill_p.add_argument("--hours", type=int, default=24, help="Lookback window (default: 24)")
    sess_sub.add_parser(
        "reconcile",
        help="Walk running claude processes and flip matched registry entries to alive",
    )

    args = parser.parse_args(_argv)

    if args.list_profiles:
        list_profiles()
        return

    if args.query:
        query_active_profile()
        return

    if not args.command:
        parser.print_help()
        sys.exit(1)

    if args.command == "version":
        print(f"agentihooks {_get_version()}")
    elif args.command == "update":
        import subprocess as _sp

        ver_before = _get_version()
        print(f"Current version: {ver_before}")
        if _is_source_checkout():
            # Editable reinstall from the source tree
            cmd = ["uv", "tool", "install", "--editable", "--force", str(AGENTIHOOKS_ROOT)]
        else:
            # Wheel install — pull latest from PyPI via uv tool, fall back to pip
            if shutil.which("uv"):
                cmd = ["uv", "tool", "install", "--force", "agentihooks"]
            else:
                cmd = [sys.executable, "-m", "pip", "install", "--upgrade", "agentihooks"]
        print(f"Running: {' '.join(cmd)}")
        result = _sp.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            ver_after = _get_version()
            if ver_after != ver_before:
                print(f"Updated: {ver_before} -> {ver_after}")
            else:
                print("Already up to date.")
            # Update state.json with new version
            state = _load_state()
            state["version"] = ver_after
            state["updated_at"] = datetime.now(timezone.utc).isoformat()
            _save_state(state)
        else:
            print(f"Update failed:\n{result.stderr}", file=sys.stderr)
            sys.exit(1)
    elif args.command == "uninstall":
        uninstall_global(args)
    elif args.command == "bundle":
        cmd_bundle(args.action, path=args.bundle_path, rebase=args.rebase)
    elif args.command == "link-profile":
        if args.action == "link":
            cmd_link_profile(
                "link",
                path=args.target,
                name=args.name,
                no_append=args.no_append,
                no_init=args.no_init,
            )
        elif args.action == "unlink":
            cmd_link_profile(
                "unlink",
                name=args.target or args.name,
                no_init=args.no_init,
            )
        elif args.action == "list":
            cmd_link_profile("list")
    elif args.command == "init":
        args.profile = getattr(args, "init_profile", None)
        args.settings_profile = getattr(args, "init_settings_profile", None) or ""
        cmd_init_unified(args)
    elif args.command == "settings-profile":
        _cmd_settings_profile(args)
    elif args.command == "ignore":
        cmd_ignore(Path(args.path).expanduser().resolve(), force=args.force)
    elif args.command == "claude":
        # Pass everything after "claude" as extra args
        try:
            idx = sys.argv.index("claude")
            extra = sys.argv[idx + 1 :]
        except ValueError:
            extra = []
        cmd_claude(extra)
    elif args.command == "lint-claude":
        sys.path.insert(0, str(AGENTIHOOKS_ROOT))
        from scripts.claude_linter import format_report, lint_report

        lint_path = (
            Path(args.lint_path).expanduser().resolve() if args.lint_path else Path.home() / ".claude" / "CLAUDE.md"
        )
        if not lint_path.exists():
            print(f"Error: {lint_path} not found", file=sys.stderr)
            sys.exit(1)
        report = lint_report(lint_path)
        print(format_report(report))
    elif args.command == "extract-skill":
        sys.path.insert(0, str(AGENTIHOOKS_ROOT))
        from scripts.claude_linter import extract_to_skill

        source = Path(args.source).expanduser().resolve() if args.source else Path.home() / ".claude" / "CLAUDE.md"
        if not source.exists():
            print(f"Error: {source} not found", file=sys.stderr)
            sys.exit(1)
        output_dir = Path(args.output_dir).expanduser().resolve() if args.output_dir else None
        try:
            result = extract_to_skill(source, args.section, args.name, output_dir)
            print(f'Extracted "{args.section}" → {result}')
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
    elif args.command == "mcp":
        sys.path.insert(0, str(AGENTIHOOKS_ROOT))
        from scripts.mcp_reporter import generate_report, load_all_mcp_configs

        servers = load_all_mcp_configs(args.project)
        print(generate_report(servers))
    elif args.command == "status":
        sys.path.insert(0, str(AGENTIHOOKS_ROOT))
        from scripts.status_checker import format_cli, run_all_checks

        print(format_cli(run_all_checks()))
    elif args.command == "doctor":
        sys.path.insert(0, str(AGENTIHOOKS_ROOT))
        from scripts.status_checker import (
            check_hook_injection,
            format_cli,
            format_hook_injection,
            run_all_checks,
        )

        if args.debug_hook:
            result = check_hook_injection()
            if args.json:
                import json as _json

                print(_json.dumps(result, indent=2))
            else:
                print(format_hook_injection(result))
            sys.exit(0 if result.get("ok") else 1)
        else:
            print(format_cli(run_all_checks()))
    elif args.command == "prune":
        known_servers_file = AGENTIHOOKS_STATE_DIR / "known-mcp-servers.json"
        valid = _get_valid_mcp_names()
        print(f"Valid MCP servers ({len(valid)}): {', '.join(sorted(valid))}")
        summary = _prune_stale_mcp_servers(known_servers_file, verbose=True)
        total = (
            summary["pruned_orphaned"]
            + summary["pruned_disabled"]
            + summary["pruned_known"]
            + summary["pruned_enabled"]
        )
        if total == 0:
            print("No stale MCP entries found — everything is clean.")
        else:
            print(f"\nPruned {total} stale entries:")
            if summary["pruned_orphaned"]:
                print(f"  mcpServers (~/.claude.json): {summary['pruned_orphaned']} orphaned server(s)")
            if summary["pruned_disabled"]:
                print(
                    f"  disabledMcpServers: {summary['pruned_disabled']} entries from {summary['projects_touched']} project(s)"
                )
            if summary["pruned_known"]:
                print(f"  known-mcp-servers.json: {summary['pruned_known']} entries")
            if summary["pruned_enabled"]:
                print(f"  enabled_mcps (state.json): {summary['pruned_enabled']} entries")
    elif args.command == "migrate":
        cmd_migrate(args)
    elif args.command == "broadcast":
        _cmd_broadcast(args)
    elif args.command == "channel":
        _cmd_channel(args)
    elif args.command == "enforcement":
        _cmd_enforcement(args)
    elif args.command == "brain":
        _cmd_brain(args)
    elif args.command == "refresh-rules":
        _cmd_refresh_rules(args)
    elif args.command == "sessions":
        _cmd_sessions(args)


def _cmd_sessions(args) -> None:
    from scripts.session_registry import cmd_list, cmd_reopen

    action = getattr(args, "sessions_action", None) or "list"
    from scripts.session_registry import cmd_backfill, cmd_reconcile

    if action in ("list", "ls"):
        cmd_list(args)
    elif action in ("reopen", "open"):
        cmd_reopen(args)
    elif action == "backfill":
        cmd_backfill(args)
    elif action == "reconcile":
        cmd_reconcile(args)
    else:
        print(f"Unknown sessions action: {action}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
