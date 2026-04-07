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
    agentihooks init --repo /path/to/repo    # per-repo config

Commands:
    agentihooks init [--bundle PATH] [--profile NAME] [--repo PATH]
        First-time setup or re-install. Links a bundle, selects a profile,
        and installs everything into ~/.claude: hooks, settings, skills,
        agents, commands, rules, CLAUDE.md, and MCP servers.

    agentihooks uninstall [--yes]
        Remove all agentihooks artifacts: symlinks, settings, CLAUDE.md,
        MCP servers, and the CLI. Preserves ~/.agentihooks/state.json.

    agentihooks daemon [start|stop|status|logs]
        Manage the sync daemon (auto-propagates source changes).

    agentihooks quota [auth|status|stop|logs]
        Manage the Claude.ai console quota watcher.

    agentihooks prune [-v]
        Remove stale MCP entries from disabledMcpServers, known-mcp-servers.json,
        and settings.local.json. Also runs automatically on every daemon cycle.

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
    ├── profile.yml                  # agentihooks metadata
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
import shutil
import signal
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
AGENTIHOOKS_STATE_DIR = Path.home() / ".agentihooks"
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
    save_json(STATE_JSON, state)


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
# Target registry (sync daemon)
# ---------------------------------------------------------------------------

_SYNC_LOCK_FILE = AGENTIHOOKS_STATE_DIR / "sync.lock"


def _register_target_global(profile: str, settings_profile: str = "") -> None:
    """Record the global install as a sync daemon target."""
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
    """Record a project install as a sync daemon target."""
    state = _load_state()
    targets = state.setdefault("targets", {})
    projects = targets.setdefault("projects", {})
    projects[str(project_path)] = {
        "profile": profile,
        "installed_at": datetime.now(timezone.utc).isoformat(),
    }
    _save_state(state)


def _unregister_target_project(project_path: Path) -> None:
    """Remove a project from the sync daemon targets."""
    state = _load_state()
    targets = state.get("targets", {})
    projects = targets.get("projects", {})
    projects.pop(str(project_path), None)
    _save_state(state)


@contextlib.contextmanager
def _sync_lock(*, blocking: bool = True):
    """Advisory file lock for install/sync operations.

    The sync daemon uses ``blocking=False`` and skips the cycle on contention.
    Manual installs use the default ``blocking=True`` to wait for the lock.
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


def _get_bundle_path() -> Path | None:
    """Return the linked bundle path from state.json, or None."""
    state = _load_state()
    bundle = state.get("bundle")
    if bundle:
        p = Path(bundle["path"])
        if p.is_dir():
            return p
    return None


def _resolve_profile_dir(profile_name: str) -> Path | None:
    """Resolve a profile name to its directory — built-in first, then bundle."""
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
    return None


def _resolve_profile_chain(profile_input: str) -> list[tuple[str, Path]]:
    """Resolve a comma-separated profile chain to a list of (name, path) tuples.

    Returns an empty list if any profile in the chain cannot be resolved.
    """
    chain = [p.strip() for p in profile_input.split(",") if p.strip()]
    if not chain:
        return []
    dirs: list[tuple[str, Path]] = []
    for name in chain:
        d = _resolve_profile_dir(name)
        if d is None:
            _cprint(f"  [WARN] Profile '{name}' in chain '{profile_input}' not found — skipping chain")
            return []
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
                desc = _read_profile_description(profiles_dir / name)
                desc_str = f" — {desc}" if desc else ""
                print(f"  {name}{desc_str}")
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


def cmd_init_unified(args: argparse.Namespace) -> None:
    """Unified init command — routes to global or per-repo install.

    agentihooks init --bundle <path>    → link bundle + global install
    agentihooks init                    → re-run global install (bundle must be linked)
    agentihooks init --repo <path>      → per-repo config with profile picker
    """
    bundle_path = getattr(args, "bundle", None)
    repo_path = getattr(args, "repo", None)

    if repo_path:
        # Per-repo mode — delegate to existing cmd_init
        cmd_init(args)
        return

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

    # Check bundle (optional — works without one using built-in profiles only)
    bundle = _get_bundle_path()
    if not bundle:
        print(f"{_DIM}[--] No bundle linked — using built-in profiles only.{_RESET}")

    # Resolve profile
    profile_name = args.profile
    if not profile_name:
        interactive = sys.stdin.isatty()
        if interactive:
            available = _available_profiles()
            print(f"Available profiles: {', '.join(available)}")
            default_profile = os.environ.get("AGENTIHOOKS_PROFILE", "default")
            profile_name = input(f"Profile [{default_profile}]: ").strip() or default_profile
        else:
            profile_name = os.environ.get("AGENTIHOOKS_PROFILE", "default")

    # Resolve settings profile (optional overlay)
    settings_profile = getattr(args, "settings_profile", None)
    if not settings_profile:
        settings_profile = os.environ.get("AGENTIHOOKS_SETTINGS_PROFILE", "")

    # Build args for _install_global_inner
    global_args = argparse.Namespace(profile=profile_name, settings_profile=settings_profile or "")
    install_global(global_args)

    # --- Auto-start daemons if accounts/config exist ---
    accounts_dir = AGENTIHOOKS_STATE_DIR / "quota-accounts"
    if accounts_dir.exists() and any(accounts_dir.glob("*.json")):
        pid_file = AGENTIHOOKS_STATE_DIR / "quota-watcher.pid"
        already_running = False
        if pid_file.exists():
            try:
                pid = int(pid_file.read_text().strip())
                os.kill(pid, 0)
                already_running = True
            except (ProcessLookupError, ValueError, PermissionError):
                pid_file.unlink(missing_ok=True)
        if not already_running:
            watcher = AGENTIHOOKS_ROOT / "scripts" / "claude_usage_watcher.py"
            python = str(_detect_venv() or sys.executable)
            if watcher.exists():
                import subprocess

                proc = subprocess.Popen(
                    [python, str(watcher)],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
                _cprint(f"\n{_GREEN}[OK] Quota daemon started (PID {proc.pid}).{_RESET}")
        else:
            print(f"\n{_DIM}[--] Quota daemon already running.{_RESET}")

    # --- Restart sync daemon (always restart on init to pick up code changes) ---
    sync_pid_file = AGENTIHOOKS_STATE_DIR / "sync-daemon.pid"
    old_pid = None
    if sync_pid_file.exists():
        try:
            old_pid = int(sync_pid_file.read_text().strip())
            os.kill(old_pid, 0)  # check if alive
        except (ProcessLookupError, ValueError, PermissionError):
            old_pid = None
            sync_pid_file.unlink(missing_ok=True)

    sync_script = AGENTIHOOKS_ROOT / "scripts" / "sync_daemon.py"
    if sync_script.exists():
        import subprocess as _sp2

        # Kill old daemon so the new one picks up code changes
        if old_pid is not None:
            try:
                os.kill(old_pid, signal.SIGTERM)
                _cprint(f"{_DIM}[--] Stopped old sync daemon (PID {old_pid}).{_RESET}")
            except (ProcessLookupError, PermissionError):
                pass
            sync_pid_file.unlink(missing_ok=True)

        python = str(_detect_venv() or sys.executable)
        proc = _sp2.Popen(
            [python, str(sync_script)],
            stdin=_sp2.DEVNULL,
            stdout=_sp2.DEVNULL,
            stderr=_sp2.DEVNULL,
            start_new_session=True,
        )
        _cprint(f"[OK] Sync daemon started (PID {proc.pid}).")

    # --- Update bashrc block (agentienv + agenti alias) ---
    if _ENV_FILE_DST.is_file():
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
        f'  if [[ ! -f "{env_file}" ]]; then\n'
        f"    return 0\n"
        f"  fi\n"
        f"  set -a\n"
        f'  . "{env_file}" 2>/dev/null || {{ set +a; return 1; }}\n'
        f"  local _c=1\n"
        f'  for f in "{env_dir}"/*.env; do\n'
        f'    [[ -f "$f" ]] && [[ "$f" != "{env_file}" ]] && {{ . "$f" 2>/dev/null; _c=$((_c + 1)); }}\n'
        f"  done\n"
        f"  set +a\n"
        f'  echo "[agentienv] loaded $_c env file(s) from {env_dir}"\n'
        f"}}\n"
        f"agentienv\n"
        f'command -v agentihooks >/dev/null 2>&1 && alias agenti="agentihooks claude"\n'
        f"{_BLOCK_END}\n"
    )

    # Always remove first, then append at the end
    _remove_bashrc_block()
    bashrc_text = _BASHRC.read_text(encoding="utf-8") if _BASHRC.exists() else ""
    sep = "\n" if bashrc_text and not bashrc_text.endswith("\n") else ""
    _BASHRC.write_text(bashrc_text + sep + block, encoding="utf-8")
    _cprint(f"{_YELLOW}[OK] agentihooks block written to {_BASHRC}{_RESET}")
    print(f"{_DIM}     run: source ~/.bashrc{_RESET}")


def cmd_init(args: argparse.Namespace) -> None:
    """Set up per-repo agentihooks config → .claude/settings.local.json."""
    repo_dir = Path(args.repo).expanduser().resolve() if args.repo else Path.cwd()
    config_path = repo_dir / ".agentihooks.json"
    profile_arg = args.profile
    interactive = sys.stdin.isatty()

    # Create .agentihooks.json if missing
    if not config_path.exists():
        if not profile_arg:
            if interactive:
                available = _available_profiles()
                print(f"Available profiles: {', '.join(available)}")
                profile_arg = input(f"Profile for this repo [{available[0] if available else 'default'}]: ").strip()
            if not profile_arg:
                profile_arg = "default"

        config = {"profile": profile_arg}
        config_path.write_text(json.dumps(config, indent=2) + "\n")
        _cprint(f"[OK] Created {config_path}")
    else:
        config = json.loads(config_path.read_text())
        if profile_arg:
            config["profile"] = profile_arg
        _cprint(f"[OK] Read {config_path}")

    # Resolve and write .claude/settings.local.json
    _write_project_settings(repo_dir, config, dry_run=getattr(args, "dry_run", False))


def _write_project_settings(repo_dir: Path, config: dict, *, dry_run: bool = False) -> None:
    """Build and write .claude/settings.local.json from per-repo config."""
    profile_name = config.get("profile", "default")

    # Resolve profile chain (comma-separated) or single profile
    profile_dirs = _resolve_profile_chain(profile_name)
    if not profile_dirs:
        _cprint(f"  [WARN] Profile '{profile_name}' not found — using default")
        profile_dirs = _resolve_profile_chain("default")
        if not profile_dirs:
            profile_dirs = [("default", PROFILES_DIR / "default")]
        profile_name = "default"

    # Load profile overrides (merged across chain)
    profile_overrides: dict = {}
    for _pname, pdir in profile_dirs:
        overrides_path = pdir / _CLAUDE_SUBDIR / "settings.overrides.json"
        if not overrides_path.exists():
            overrides_path = pdir / "settings.overrides.json"
        if overrides_path.exists():
            try:
                ovr = load_json(overrides_path)
                # Deep merge: dict keys merge additively, scalars overwrite
                for key in ("env", "permissions"):
                    if key in ovr:
                        if key in profile_overrides and isinstance(profile_overrides[key], dict):
                            profile_overrides[key] = {**profile_overrides[key], **ovr[key]}
                        else:
                            profile_overrides[key] = ovr[key]
                for k, v in ovr.items():
                    if k not in ("env", "permissions"):
                        profile_overrides[k] = v
            except (json.JSONDecodeError, OSError):
                pass

    # Apply settings-profile overlay if specified in .agentihooks.json
    sp_name = config.get("settings_profile", "")
    if sp_name:
        sp_dir = _resolve_profile_dir(sp_name)
        if sp_dir is not None:
            sp_overrides = sp_dir / _CLAUDE_SUBDIR / "settings.overrides.json"
            if not sp_overrides.exists():
                sp_overrides = sp_dir / "settings.overrides.json"
            if sp_overrides.exists():
                try:
                    ovr = load_json(sp_overrides)
                    # Deep merge: dict keys merge additively, scalars overwrite
                    for key in ("env", "permissions"):
                        if key in ovr:
                            if key in profile_overrides and isinstance(profile_overrides[key], dict):
                                profile_overrides[key] = {**profile_overrides[key], **ovr[key]}
                            else:
                                profile_overrides[key] = ovr[key]
                    for k, v in ovr.items():
                        if k not in ("env", "permissions"):
                            profile_overrides[k] = v
                    _cprint(f"  [OK] Applied settings-profile '{sp_name}' overlay (project-level)")
                except (json.JSONDecodeError, OSError):
                    pass

    # Load connector rules for this profile (use primary profile name)
    primary_profile = profile_dirs[-1][0]
    conn_env, conn_deny, conn_disabled = _load_connectors(primary_profile)

    # Per-repo overrides from .agentihooks.json
    repo_disabled = config.get("disabledMcpServers", [])
    repo_deny = config.get("permissions", {}).get("deny", [])
    repo_ask = config.get("permissions", {}).get("ask", [])
    repo_env = config.get("env", {})

    # Build settings.local.json — only the delta
    local_settings: dict = {}

    # Disabled MCP servers (project-scope .mcp.json only — for connectors)
    all_disabled = list(dict.fromkeys(conn_disabled + repo_disabled))
    if all_disabled:
        local_settings["disabledMcpjsonServers"] = all_disabled

    # Blacklist-all-by-default: disable every known MCP in ~/.claude.json
    # projects block, except those the profile or repo whitelists.
    all_known = _get_all_known_mcp_names()
    # Union enabledMcpServers across all profiles in the chain
    profile_enabled: set[str] = set()
    for _pname, pdir in profile_dirs:
        pe = _get_profile_enabled_servers(pdir)
        if pe:
            profile_enabled |= pe
    repo_enabled = set(config.get("enabledMcpServers", []))
    # Also respect child project whitelists so parent never blocks their servers
    try:
        all_project_paths = set(load_json(_CLAUDE_JSON).get("projects", {}).keys()) if _CLAUDE_JSON.exists() else set()
    except (json.JSONDecodeError, OSError):
        all_project_paths = set()
    child_enabled = _collect_child_enabled_mcps(repo_dir, all_project_paths)
    to_disable = sorted(all_known - profile_enabled - repo_enabled - child_enabled)
    if not dry_run and all_known:
        _write_project_disabled_mcps(repo_dir, to_disable)

    # Permissions — merge profile + connector + repo overrides
    perms: dict = {}
    all_deny = profile_overrides.get("permissions", {}).get("deny", []) + conn_deny + repo_deny
    all_ask = profile_overrides.get("permissions", {}).get("ask", []) + repo_ask
    default_mode = profile_overrides.get("permissions", {}).get("defaultMode")

    if all_deny:
        perms["deny"] = list(dict.fromkeys(all_deny))
    if all_ask:
        perms["ask"] = list(dict.fromkeys(all_ask))
    if default_mode:
        perms["defaultMode"] = default_mode
    if perms:
        local_settings["permissions"] = perms

    # Env — merge profile + connector + repo + otel
    all_env = {}
    all_env.update(profile_overrides.get("env", {}))
    all_env.update(conn_env)
    all_env.update(repo_env)

    # OTEL overrides from .agentihooks.json otel section
    otel_cfg = config.get("otel", {})
    if otel_cfg:
        otel_env = _build_otel_env({"otel": otel_cfg})
        all_env.update(otel_env)

    if all_env:
        local_settings["env"] = all_env

    if dry_run:
        print(json.dumps(local_settings, indent=2))
        return

    # Write
    claude_dir = repo_dir / ".claude"
    claude_dir.mkdir(exist_ok=True)
    out_path = claude_dir / "settings.local.json"
    save_json(out_path, local_settings)

    _cprint(f"[OK] Wrote {out_path}")
    print(f"     Profile: {profile_name}")
    if all_disabled:
        print(f"     Disabled .mcp.json servers: {all_disabled}")
    all_enabled = profile_enabled | repo_enabled
    if all_enabled:
        print(f"     Enabled MCPs ({len(all_enabled)}): {sorted(all_enabled)}")
    if to_disable:
        print(f"     Blacklisted MCPs ({len(to_disable)}): {to_disable[:5]}{'...' if len(to_disable) > 5 else ''}")
    if all_deny:
        print(f"     Deny rules: {len(all_deny)}")
    if all_ask:
        print(f"     Ask rules: {len(all_ask)}")
    if all_env:
        print(f"     Env vars: {len(all_env)}")

    # Generate CLAUDE.local.md from profile chain
    if not dry_run:
        claude_local_md = claude_dir / "CLAUDE.local.md"
        claude_md_parts: list[str] = []
        for pname, pdir in profile_dirs:
            src = pdir / _CLAUDE_MD_NAME
            if src.exists():
                content = src.read_text().strip()
                if content:
                    if len(profile_dirs) > 1:
                        claude_md_parts.append(f"<!-- profile: {pname} -->\n{content}")
                    else:
                        claude_md_parts.append(content)
        if claude_md_parts:
            claude_local_md.write_text("\n\n---\n\n".join(claude_md_parts) + "\n")
            sources = [pn for pn, pd in profile_dirs if (pd / _CLAUDE_MD_NAME).exists()]
            _cprint(f"  [OK] Wrote CLAUDE.local.md ({' + '.join(sources)})")

    # Ensure .gitignore covers settings.local.json and CLAUDE.local.md
    _ensure_local_settings_gitignored(repo_dir)


def _ensure_local_settings_gitignored(repo_dir: Path) -> None:
    """Ensure .claude/settings.local.json and CLAUDE.local.md are gitignored."""
    entries = [".claude/settings.local.json", ".claude/CLAUDE.local.md"]
    gitignore = repo_dir / ".gitignore"
    if gitignore.exists():
        content = gitignore.read_text()
        added = []
        for entry in entries:
            if entry not in content:
                added.append(entry)
        if added:
            with open(gitignore, "a") as f:
                f.write("\n" + "\n".join(added) + "\n")
            _cprint(f"  [OK] Added {', '.join(added)} to .gitignore")
    # Don't create .gitignore if it doesn't exist — not our file to create


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
        _cprint(f"  [!!] .env.example not found — could not seed {_ENV_FILE_DST}")


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
        f'command -v agentihooks >/dev/null 2>&1 && alias agenti="agentihooks claude"\n'
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

    ~/.agentihooks/.venv always wins when it exists — it is the canonical
    environment regardless of which project venv happens to be activated.
    """
    # 1. Dedicated ~/.agentihooks/.venv — always preferred when present
    agentihooks_venv = Path.home() / ".agentihooks" / ".venv" / "bin" / "python"
    if agentihooks_venv.exists():
        return agentihooks_venv

    # 2. Activated venv via VIRTUAL_ENV (fallback if dedicated venv missing)
    venv_env = os.environ.get("VIRTUAL_ENV")
    if venv_env:
        python = Path(venv_env) / "bin" / "python"
        if python.exists():
            return python

    # 3. .venv directory in cwd
    local_venv = Path.cwd() / ".venv" / "bin" / "python"
    if local_venv.exists():
        return local_venv

    return None


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
    """Return profile names from built-in profiles/ and linked bundle."""
    names = {d.name for d in PROFILES_DIR.iterdir() if d.is_dir() and not d.name.startswith("_")}
    bundle = _get_bundle_path()
    if bundle:
        bp = bundle / "profiles"
        if bp.is_dir():
            names.update(d.name for d in bp.iterdir() if d.is_dir() and not d.name.startswith("_"))
    return sorted(names)


def _read_profile_field(profile_dir: Path, field: str) -> str:
    """Read a top-level field from profile.yml (simple key: value only)."""
    yml = profile_dir / "profile.yml"
    if not yml.exists():
        return ""
    prefix = f"{field}:"
    for line in yml.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith(prefix):
            return stripped[len(prefix) :].strip().strip('"').strip("'")
    return ""


def _read_profile_description(profile_dir: Path) -> str:
    """Return the description field from profile.yml, or '' if absent."""
    return _read_profile_field(profile_dir, "description")


def query_active_profile() -> None:
    """Print the active profile for the current directory, falling back to global."""
    # Check CWD for .agentihooks.json first
    cwd = Path.cwd()
    local_config = cwd / ".agentihooks.json"
    source = "global"
    profile_name = None

    if local_config.exists():
        try:
            cfg = load_json(local_config)
            profile_name = cfg.get("profile")
            if profile_name:
                source = "local"
        except (json.JSONDecodeError, OSError):
            pass

    # Fall back to global state
    if not profile_name:
        state = _load_state()
        targets = state.get("targets", {})
        global_target = targets.get("global", {})
        profile_name = global_target.get("profile")

    if not profile_name:
        print("not installed")
        return

    chain = [p.strip() for p in profile_name.split(",") if p.strip()]
    if len(chain) > 1:
        print(f"chain: [{', '.join(chain)}] ({source})")
    else:
        print(f"{profile_name} ({source})")


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
        desc = _read_profile_description(profile_dir)
        # Determine source
        if (PROFILES_DIR / name).is_dir():
            source = "built-in"
        else:
            source = "bundle"
        marker = ""
        claude_md = profile_dir / _CLAUDE_MD_NAME
        if not claude_md.exists():
            marker = f"  [no {_CLAUDE_MD_NAME}]"
        desc_str = f" — {desc}" if desc else ""
        print(f"  {name} ({source}){desc_str}{marker}")
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

    # Validate all profiles exist
    profile_dirs: list[tuple[str, Path]] = []
    for pname in profile_chain:
        pdir = _resolve_profile_dir(pname)
        if pdir is None:
            available = _available_profiles()
            print(f"ERROR: Profile '{pname}' not found.", file=sys.stderr)
            print(f"Available profiles: {', '.join(available)}", file=sys.stderr)
            sys.exit(1)
        profile_dirs.append((pname, pdir))

    # For display and state storage, use the full chain string
    profile_name = ",".join(profile_chain)

    profile_sources = []
    for pname, _ in profile_dirs:
        src = "built-in" if (PROFILES_DIR / pname).is_dir() else "bundle"
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
        sp_src = "built-in" if (PROFILES_DIR / _settings_profile_display).is_dir() else "bundle"
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
            src = "built-in" if (PROFILES_DIR / settings_profile_name).is_dir() else "bundle"
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

    # --- 1d. OTEL baseline env vars (from last profile with a profile.yml) ---
    for _pname, pdir in reversed(profile_dirs):
        profile_yml_path = pdir / "profile.yml"
        if profile_yml_path.exists():
            import yaml

            profile_data = yaml.safe_load(profile_yml_path.read_text()) or {}
            otel_env = _build_otel_env(profile_data)
            if otel_env:
                rendered.setdefault("env", {}).update(otel_env)
                _cprint(f"  [OK] OTEL env: {list(otel_env.keys())}")
            break

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

    # --- 5. Install CLAUDE.md (first profile = symlink, rest = rules) ---
    _cleanup_stale_claude_md_symlink()
    # Remove any previous chain-injected CLAUDE.md rules
    rules_dir = CLAUDE_HOME / "rules"
    if rules_dir.is_dir():
        for f in rules_dir.iterdir():
            if f.name.startswith("_profile-") and f.name.endswith(".md") and f.is_symlink():
                f.unlink()

    if len(profile_chain) == 1:
        # Single profile: symlink as before
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
            # Remove stale symlink if present
            if dst.is_symlink():
                dst.unlink()
            dst.write_text("\n\n---\n\n".join(claude_md_parts) + "\n")
            sources = [pn for pn, pd in profile_dirs if (pd / _CLAUDE_MD_NAME).exists()]
            _cprint(f"[OK] Wrote chained CLAUDE.md ({' + '.join(sources)})")
        else:
            # No CLAUDE.md in any profile — try last profile as fallback
            _install_system_prompt(profile_dirs[-1][1], profile_dirs[-1][0])

    # --- 6. Install MCP servers to user scope (~/.claude.json) ---
    # Layer 1: hooks-utils from agentihooks
    _install_user_mcp(last_profile)
    # Layer 2: bundle .claude/.mcp.json (all profiles), fallback to root .mcp.json
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

    # --- 9c. Blacklist all MCPs across all projects ---
    print()
    print("Applying MCP blacklist to all projects...")
    _blacklist_all_projects_mcps(profile_dirs[-1][1])

    # --- 10. Install agentihooks CLI tool to ~/.local/bin ---
    print()
    _install_cli_tool()

    # --- 11. Seed ~/.agentihooks/.env from .env.example (first run only) ---
    print()
    _seed_user_env_file()

    # --- 12. Register as sync daemon target ---
    _register_target_global(profile_name, settings_profile=settings_profile_name)
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


def _get_profile_enabled_servers(profile_dir: Path) -> set[str] | None:
    """Read enabledMcpServers whitelist from profile.yml.

    Returns set of server names to keep enabled, or None if field absent.
    """
    yml_path = profile_dir / "profile.yml"
    if not yml_path.exists():
        return None
    try:
        import yaml

        data = yaml.safe_load(yml_path.read_text()) or {}
        enabled = data.get("enabledMcpServers")
        if enabled is None:
            return None
        return set(enabled) if enabled else set()
    except OSError:
        return None
    except Exception as exc:
        _cprint(f"  [WARN] Could not parse enabledMcpServers from {yml_path}: {exc}")
        return None


def _write_project_disabled_mcps(repo_path: Path, disabled_names: list[str]) -> None:
    """Write disabledMcpServers to ~/.claude.json projects[path] block.

    Respects per-project user-enabled MCPs tracked in state.json —
    any server the user manually enabled for this project stays enabled.
    """
    if not _CLAUDE_JSON.exists():
        return
    try:
        # Read per-project enabled MCPs from state and subtract them
        state = _load_state()
        proj_state = state.get("targets", {}).get("projects", {}).get(str(repo_path), {})
        user_enabled = set(proj_state.get("enabled_mcps", []))
        final_disabled = sorted(set(disabled_names) - user_enabled)

        data = load_json(_CLAUDE_JSON)
        projects = data.setdefault("projects", {})
        proj = projects.setdefault(str(repo_path), {})
        proj["disabledMcpServers"] = final_disabled
        save_json(_CLAUDE_JSON, data)
        kept = len(disabled_names) - len(final_disabled)
        msg = f"  [OK] Wrote disabledMcpServers to ~/.claude.json ({len(final_disabled)} servers)"
        if kept:
            msg += f" — kept {kept} user-enabled"
        _cprint(msg)
    except (json.JSONDecodeError, OSError) as e:
        _cprint(f"  [WARN] Could not update ~/.claude.json: {e}")


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


def _collect_child_enabled_mcps(parent_path: Path, all_project_paths: set[str]) -> set[str]:
    """Return the union of enabledMcpServers from child project .agentihooks.json files.

    When a parent project computes its disabledMcpServers, it must not block
    servers that child projects explicitly whitelist — otherwise Claude Code's
    upward settings resolution will override the child's whitelist.
    """
    prefix = str(parent_path) + "/"
    enabled: set[str] = set()
    for candidate in all_project_paths:
        if not candidate.startswith(prefix):
            continue
        child_config = Path(candidate) / ".agentihooks.json"
        if not child_config.exists():
            continue
        try:
            data = load_json(child_config)
            enabled.update(data.get("enabledMcpServers", []))
        except (json.JSONDecodeError, OSError):
            pass
    return enabled


def _blacklist_all_projects_mcps(profile_dir: Path) -> None:
    """Blacklist all known MCPs in every project entry in ~/.claude.json.

    Called by 'agentihooks init' to ensure every project starts with all
    MCPs disabled except those the profile whitelists.
    """
    if not _CLAUDE_JSON.exists():
        return
    try:
        data = load_json(_CLAUDE_JSON)
    except (json.JSONDecodeError, OSError):
        return

    projects = data.get("projects", {})
    if not projects:
        return

    all_known = _get_all_known_mcp_names()
    if not all_known:
        return

    profile_enabled = _get_profile_enabled_servers(profile_dir) or set()
    base_disabled = all_known - profile_enabled

    # Read per-project enabled MCPs from state so we don't overwrite user choices
    state = _load_state()
    state_projects = state.get("targets", {}).get("projects", {})
    all_project_paths = set(projects.keys())

    updated = 0
    for proj_path, proj_data in projects.items():
        if not isinstance(proj_data, dict):
            continue
        user_enabled = set(state_projects.get(proj_path, {}).get("enabled_mcps", []))
        child_enabled = _collect_child_enabled_mcps(Path(proj_path), all_project_paths)
        # Read this project's own .agentihooks.json whitelist + profile override
        own_enabled: set[str] = set()
        own_config = Path(proj_path) / ".agentihooks.json"
        if own_config.exists():
            try:
                cfg = load_json(own_config)
                own_enabled = set(cfg.get("enabledMcpServers", []))
                # If project specifies a profile, use its enabledMcpServers too
                proj_profile = cfg.get("profile")
                if proj_profile:
                    for _pname, pdir in _resolve_profile_chain(proj_profile):
                        pe = _get_profile_enabled_servers(pdir)
                        if pe:
                            own_enabled |= pe
            except (json.JSONDecodeError, OSError):
                pass
        proj_data["disabledMcpServers"] = sorted(base_disabled - user_enabled - child_enabled - own_enabled)
        updated += 1

    if updated:
        save_json(_CLAUDE_JSON, data)
        _cprint(
            f"  [OK] Blacklisted MCPs across {updated} project(s) in ~/.claude.json (respecting per-project enables)"
        )


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
    """Build MCP server config for the hooks-utils server."""
    return {
        "mcpServers": {
            "hooks-utils": {
                "command": sys.executable,
                "args": ["-m", "hooks.mcp"],
                "cwd": str(AGENTIHOOKS_ROOT),
                "env": {"MCP_CATEGORIES": mcp_categories},
            }
        }
    }


def _install_user_mcp(profile_name: str) -> None:
    """Generate and merge MCP server config into ~/.claude.json.

    Reads ``mcp_categories`` from ``profile.yml`` (defaults to ``all``)
    and builds the hooks-utils MCP server config dynamically.
    """
    profile_dir = _resolve_profile_dir(profile_name) or PROFILES_DIR / profile_name
    mcp_categories = _read_profile_field(profile_dir, "mcp_categories") or "all"
    mcp_config = _build_mcp_config(mcp_categories)
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


def sync_user_mcp() -> None:
    """Re-apply all MCP files tracked in ~/.agentihooks/state.json.

    Skips paths that no longer exist (with a warning) so a missing
    repo doesn't abort the whole sync.
    """
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

    result = subprocess.run(
        [uv, "tool", "install", "--editable", "--force", "."],
        cwd=AGENTIHOOKS_ROOT,
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        _cprint("  [OK] CLI installed via: uv tool install --editable .")
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


def _install_system_prompt(profile_dir: Path, profile_name: str) -> None:
    """Copy profile's CLAUDE.md to ~/.claude/CLAUDE.md.

    Writes a real file (not a symlink) so it resolves correctly across
    WSL/Windows boundaries and VS Code \\\\wsl.localhost paths.
    The sync daemon detects source changes and re-copies automatically.
    """
    src = profile_dir / _CLAUDE_MD_NAME
    dst = CLAUDE_HOME / _CLAUDE_MD_NAME

    if not src.exists():
        _cprint(f"  [--] No {_CLAUDE_MD_NAME} in profile '{profile_name}' — skipping system prompt.")
        return

    new_content = src.read_text()

    # Check if content is already up to date
    if dst.exists() and not dst.is_symlink():
        if dst.read_text() == new_content:
            _cprint(f"  [--] {_CLAUDE_MD_NAME} already up to date (from {profile_name})")
            return

    # Remove stale symlink or backup existing file
    if dst.is_symlink():
        dst.unlink()
    elif dst.exists():
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = dst.with_suffix(f".md.bak.{timestamp}")
        shutil.copy2(dst, backup)
        print(f"  Backed up existing {_CLAUDE_MD_NAME} → {backup}")
        dst.unlink()

    dst.write_text(new_content)
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

    # --- 3. Profile .mcp.json ---
    state = _load_state()
    profile_name = state.get("targets", {}).get("global", {}).get("profile")
    if profile_name:
        profile_dir = _resolve_profile_dir(profile_name)
        if profile_dir:
            profile_mcp = profile_dir / _CLAUDE_SUBDIR / _MCP_JSON_NAME
            if profile_mcp.exists():
                try:
                    merged.update(load_json(profile_mcp).get("mcpServers", {}))
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

    # Remove CLAUDE.md if it's a symlink pointing into any profiles/ directory
    remove_claude_md = claude_md_dst.is_symlink() and "profiles/" in str(claude_md_dst.resolve())

    # Only count servers that are both managed AND actually present in ~/.claude.json
    managed_servers = _collect_all_managed_mcp_servers()
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
        print(f"  {claude_md_dst}  (stale symlink → profiles/)")
    else:
        print(f"  {claude_md_dst}  [SKIP — not a managed symlink]")
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

    # --- 5. Remove CLAUDE.md symlink (stale) + active system prompt ---
    print()
    if remove_claude_md:
        claude_md_dst.unlink()
        _cprint(f"[OK] Removed stale {claude_md_dst}")
    else:
        print(f"[--] Skipped {claude_md_dst} (not a managed symlink)")

    # --- 6. Remove MCP servers from ~/.claude.json ---
    print()
    if managed_servers:
        print(f"Removing {len(managed_servers)} MCP server(s) from {_CLAUDE_JSON}:")
        _remove_mcp_from_user_scope(managed_servers)
    else:
        _cprint(f"  [--] No managed MCP servers to remove from {_CLAUDE_JSON}")

    # --- 7. Stop quota + sync daemons ---
    print()
    if _quota_stop_daemon():
        _cprint("[OK] Quota daemon stopped.")
    else:
        print("[--] Quota daemon not running.")
    sync_pid = AGENTIHOOKS_STATE_DIR / "sync-daemon.pid"
    if sync_pid.exists():
        import signal

        try:
            pid = int(sync_pid.read_text().strip())
            os.kill(pid, signal.SIGTERM)
            sync_pid.unlink(missing_ok=True)
            _cprint(f"[OK] Sync daemon stopped (PID {pid}).")
        except (ProcessLookupError, ValueError):
            sync_pid.unlink(missing_ok=True)

    # --- 8. Remove bashrc block ---
    if _remove_bashrc_block():
        _cprint(f"[OK] Removed agentihooks block from {_BASHRC}")

    # --- 9. Uninstall CLI ---
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

    mcp_categories = _read_profile_field(profile_dir, "mcp_categories") or "all"
    rendered_mcp = _build_mcp_config(mcp_categories)

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

    # Register as sync daemon target
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


def cmd_daemon(args: "argparse.Namespace") -> None:
    """Launch or interact with the sync daemon."""
    daemon_script = AGENTIHOOKS_ROOT / "scripts" / "sync_daemon.py"
    if not daemon_script.exists():
        print(f"ERROR: sync daemon not found at {daemon_script}", file=sys.stderr)
        sys.exit(1)

    python = str(_detect_venv() or sys.executable)
    pid_file = AGENTIHOOKS_STATE_DIR / "sync-daemon.pid"
    log_file = AGENTIHOOKS_STATE_DIR / "logs" / "sync-daemon.log"
    hash_file = AGENTIHOOKS_STATE_DIR / "sync-hashes.json"

    if args.action == "logs":
        if not log_file.exists():
            print("No log file yet. Start the daemon first:  agentihooks daemon start")
            sys.exit(0)
        os.execlp("tail", "tail", "-f", str(log_file))

    elif args.action == "stop":
        stopped = []
        # Kill PID from file
        if pid_file.exists():
            try:
                pid = int(pid_file.read_text().strip())
                os.kill(pid, signal.SIGTERM)
                stopped.append(pid)
            except (ProcessLookupError, ValueError):
                pass
            pid_file.unlink(missing_ok=True)
        # Kill any orphaned daemon processes
        import subprocess as _sp

        try:
            result = _sp.run(
                ["pgrep", "-f", "sync_daemon.py.*--foreground"],
                capture_output=True,
                text=True,
            )
            for line in result.stdout.strip().splitlines():
                orphan_pid = int(line.strip())
                if orphan_pid not in stopped:
                    os.kill(orphan_pid, signal.SIGTERM)
                    stopped.append(orphan_pid)
        except (OSError, ValueError):
            pass
        if stopped:
            print(f"[sync] Daemon stopped (PID(s): {', '.join(str(p) for p in stopped)}).")
        else:
            print("[sync] No daemon running.")

    elif args.action == "status":
        state = _load_state()
        targets = state.get("targets", {})

        # PID status
        if pid_file.exists():
            try:
                pid = int(pid_file.read_text().strip())
                os.kill(pid, 0)
                print(f"[sync] Daemon running (PID {pid})")
            except (ProcessLookupError, ValueError):
                pid_file.unlink(missing_ok=True)
                print("[sync] Daemon not running (stale PID cleaned up)")
        else:
            print("[sync] Daemon not running")

        # Targets
        g = targets.get("global")
        if g:
            print(f"  Global target: {g['path']} (profile: {g['profile']})")
        else:
            print("  Global target: not registered (run 'agentihooks init' first)")
        projects = targets.get("projects", {})
        if projects:
            print(f"  Project targets: {len(projects)}")
            for p, info in projects.items():
                exists = Path(p).exists()
                marker = "" if exists else " [PATH MISSING]"
                print(f"    {p} (profile: {info['profile']}){marker}")
        else:
            print("  Project targets: none")

        # Hash file
        if hash_file.exists():
            try:
                hdata = load_json(hash_file)
                n = len(hdata.get("hashes", {}))
                print(f"  Watching {n} source file(s)")
                print(f"  Last scan: {hdata.get('_updated', 'unknown')}")
            except (json.JSONDecodeError, OSError):
                print("  Hash file: corrupt")
        else:
            print("  Hash file: not yet created")

    else:  # start
        cmd = [python, str(daemon_script)]
        if args.foreground:
            cmd.append("--foreground")
        cmd += ["--poll", str(args.poll)]
        os.execv(python, cmd)


def _quota_stop_daemon() -> bool:
    """Stop the quota daemon if running. Returns True if it was running."""
    import signal

    pid_file = Path.home() / ".agentihooks" / "quota-watcher.pid"
    if not pid_file.exists():
        return False
    try:
        pid = int(pid_file.read_text().strip())
        os.kill(pid, signal.SIGTERM)
        pid_file.unlink(missing_ok=True)
        print(f"[quota] Daemon stopped (PID {pid}).")
        return True
    except (ProcessLookupError, ValueError):
        pid_file.unlink(missing_ok=True)
        return False


def _quota_start_daemon(python: str, watcher: Path, poll: int = 60) -> None:
    """Start the quota daemon."""
    cmd = [python, str(watcher)]
    if poll != 60:
        cmd += ["--poll", str(poll)]
    os.execv(python, cmd)


def cmd_claude(extra_args: list[str]) -> None:
    """Launch claude with flags from the active profile's claude: section."""
    state = _load_state()
    profile_name = state.get("targets", {}).get("global", {}).get("profile", "default")
    profile_dir = _resolve_profile_dir(profile_name)

    claude_flags: dict = {}
    if profile_dir:
        yml = profile_dir / "profile.yml"
        if yml.exists():
            try:
                data = yaml.safe_load(yml.read_text(encoding="utf-8"))
                claude_flags = data.get("claude", {}) or {}
            except (yaml.YAMLError, OSError):
                pass

    # Map profile.yml fields → claude CLI flags
    cmd = ["claude"]

    # Permission mode — special mapping
    perm = claude_flags.get("permission_mode")
    if perm == "bypassPermissions":
        cmd.append("--dangerously-skip-permissions")
    elif perm and perm != "default":
        cmd.extend(["--permission-mode", perm])

    # Simple key→flag mappings
    for key, flag in {"model": "--model", "effort": "--effort"}.items():
        val = claude_flags.get(key)
        if val is not None:
            cmd.extend([flag, str(val)])

    # Boolean flags
    if claude_flags.get("worktree"):
        cmd.append("--worktree")

    # Append system prompt from profile
    if profile_dir:
        system_prompt = profile_dir / "CLAUDE.md"
        if system_prompt.exists():
            cmd.extend(["--append-system-prompt-file", str(system_prompt)])

    # Pass through any extra args from the user
    cmd.extend(extra_args)

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


def cmd_quota(args: "argparse.Namespace") -> None:
    """Launch or interact with the Claude.ai quota watcher."""
    watcher = AGENTIHOOKS_ROOT / "scripts" / "claude_usage_watcher.py"
    if not watcher.exists():
        print(f"ERROR: quota watcher not found at {watcher}", file=sys.stderr)
        sys.exit(1)

    python = str(_detect_venv() or sys.executable)
    accounts_dir = AGENTIHOOKS_STATE_DIR / "quota-accounts"
    legacy_auth = AGENTIHOOKS_STATE_DIR / "claude_auth_state.json"

    # Migrate legacy single-account auth to multi-account
    if not accounts_dir.exists() and legacy_auth.exists():
        accounts_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(legacy_auth, accounts_dir / "default.json")
        state = _load_state()
        state["active_quota_account"] = "default"
        _save_state(state)
        print("[quota] Migrated auth → quota-accounts/default.json")

    state = _load_state()
    active = state.get("active_quota_account", "default")

    if args.action == "auth":
        account = getattr(args, "quota_account", None)
        cmd = [python, str(watcher), "--auth"]
        if account:
            cmd += ["--account", account]
        os.execv(python, cmd)

    elif args.action == "import-cookies":
        account = getattr(args, "quota_account", None)
        cmd = [python, str(watcher), "--import-cookies"]
        if account:
            cmd += ["--account", account]
        os.execv(python, cmd)

    elif args.action == "dump-html":
        os.execv(python, [python, str(watcher), "--dump-html"])

    elif args.action == "list":
        if not accounts_dir.exists():
            print("[quota] No accounts. Run:  agentihooks quota auth <name>")
            return
        accounts = sorted(p.stem for p in accounts_dir.glob("*.json"))
        if not accounts:
            print("[quota] No accounts. Run:  agentihooks quota auth <name>")
            return
        print("Quota accounts:")
        for name in accounts:
            marker = " (active)" if name == active else ""
            print(f"  {name}{marker}")

    elif args.action == "switch":
        account = getattr(args, "quota_account", None)
        if not account:
            # Interactive picker
            if not accounts_dir.exists():
                print("[quota] No accounts. Run:  agentihooks quota auth <name>")
                sys.exit(1)
            accounts = sorted(p.stem for p in accounts_dir.glob("*.json"))
            if not accounts:
                print("[quota] No accounts. Run:  agentihooks quota auth <name>")
                sys.exit(1)
            print("Quota accounts:")
            for i, name in enumerate(accounts, 1):
                marker = " (active)" if name == active else ""
                print(f"  {i}. {name}{marker}")
            try:
                choice = input("\nSwitch to [number]: ").strip()
                idx = int(choice) - 1
                if 0 <= idx < len(accounts):
                    account = accounts[idx]
                else:
                    sys.exit("Invalid choice.")
            except (EOFError, KeyboardInterrupt, ValueError):
                sys.exit("\nAborted.")
        # Verify account exists
        if not (accounts_dir / f"{account}.json").exists():
            print(f"[quota] Account '{account}' not found. Run:  agentihooks quota auth {account}")
            sys.exit(1)
        state["active_quota_account"] = account
        _save_state(state)
        print(f"[quota] Switched to account: {account}")
        _quota_stop_daemon()
        print("[quota] Restarting daemon...")
        _quota_start_daemon(python, watcher, args.poll)

    elif args.action == "restart":
        _quota_stop_daemon()
        print("[quota] Starting daemon...")
        _quota_start_daemon(python, watcher, args.poll)

    elif args.action == "remove":
        account = getattr(args, "quota_account", None)
        if not account:
            print("Usage: agentihooks quota remove <name>")
            sys.exit(1)
        target = accounts_dir / f"{account}.json"
        if not target.exists():
            print(f"[quota] Account '{account}' not found.")
            sys.exit(1)
        if account == active:
            print(f"[quota] WARNING: '{account}' is the active account.")
            _quota_stop_daemon()
        target.unlink()
        print(f"[quota] Removed account: {account}")

    elif args.action == "logs":
        log_file = Path.home() / ".agentihooks" / "logs" / "quota-watcher.log"
        if not log_file.exists():
            print("No log file yet. Start the daemon first:  agentihooks quota")
            sys.exit(0)
        os.execlp("tail", "tail", "-f", str(log_file))

    elif args.action == "stop":
        if not _quota_stop_daemon():
            print("[quota] No daemon running.")

    elif args.action == "status":
        import json as _json

        print(f"[quota] Active account: {active}")
        usage_file = Path(os.getenv("CLAUDE_USAGE_FILE", str(Path.home() / ".agentihooks" / "claude_usage.json")))
        if not usage_file.exists():
            print("No quota data yet. Run:  agentihooks quota")
            sys.exit(0)
        data = _json.loads(usage_file.read_text())
        print(_json.dumps(data, indent=2))

    else:  # watch (default)
        # If already running, show status + available commands
        pid_file = AGENTIHOOKS_STATE_DIR / "quota-watcher.pid"
        if pid_file.exists():
            try:
                pid = int(pid_file.read_text().strip())
                os.kill(pid, 0)
                print(f"[quota] Daemon running (PID {pid}), account: {active}")
                usage_file = Path(
                    os.getenv(
                        "CLAUDE_USAGE_FILE",
                        str(AGENTIHOOKS_STATE_DIR / "claude_usage.json"),
                    )
                )
                if usage_file.exists():
                    import json as _json2

                    try:
                        data = _json2.loads(usage_file.read_text())
                        s = data.get("session", {})
                        w = (data.get("weekly") or {}).get("all_models", {})
                        parts = []
                        if s.get("used_pct") is not None:
                            parts.append(f"session:{s['used_pct']:.0f}%")
                        if w.get("used_pct") is not None:
                            parts.append(f"weekly:{w['used_pct']:.0f}%")
                        if parts:
                            print(f"  {' | '.join(parts)}")
                    except (json.JSONDecodeError, OSError):
                        pass
                print()
                print("Commands:")
                print("  agentihooks quota status       # full quota JSON")
                print("  agentihooks quota list          # show accounts")
                print("  agentihooks quota switch [NAME] # switch account")
                print("  agentihooks quota auth [NAME]   # add/update account")
                print("  agentihooks quota restart       # restart daemon")
                print("  agentihooks quota stop          # stop daemon")
                print("  agentihooks quota logs          # tail daemon log")
                print("  agentihooks quota remove NAME   # delete account")
                return
            except (ProcessLookupError, ValueError, PermissionError):
                pid_file.unlink(missing_ok=True)
        _quota_start_daemon(python, watcher, args.poll)


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

    # Re-apply MCP blacklist for all remapped projects
    all_known = _get_all_known_mcp_names()
    # Resolve active profile for the whitelist
    state = _load_state()
    active_profile = state.get("targets", {}).get("global", {}).get("profile", "default")
    profile_dir = _resolve_profile_dir(active_profile) or PROFILES_DIR / "default"
    profile_enabled = _get_profile_enabled_servers(profile_dir)
    to_disable = sorted(all_known - (profile_enabled or set()))

    all_project_paths = set(projects.keys())
    for _old, new in remap:
        proj = projects.setdefault(new, {})
        child_enabled = _collect_child_enabled_mcps(Path(new), all_project_paths)
        proj["disabledMcpServers"] = sorted(set(to_disable) - child_enabled)

    # Save
    data["projects"] = projects
    save_json(_CLAUDE_JSON, data)
    print(f"[OK] Migrated {len(remap)} project(s) in ~/.claude.json")
    print(f"[OK] Applied MCP blacklist ({len(to_disable)} servers) to migrated projects")

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

    init_p = sub.add_parser(
        "init", help="Initialize agentihooks (global setup from bundle, or per-repo config with --repo)"
    )
    init_p.add_argument(
        "--bundle", default=None, help="Path to bundle directory (first-time setup: link bundle + global install)"
    )
    init_p.add_argument("--repo", default=None, help="Target repo directory (per-repo config with profile picker)")
    init_p.add_argument(
        "--local", action="store_true", help="Shorthand for --repo . (per-repo config for current directory)"
    )
    init_p.add_argument("--profile", dest="init_profile", default=None, help="Profile to use (headless mode)")
    init_p.add_argument(
        "--settings-profile",
        dest="init_settings_profile",
        default=None,
        help="Settings-only overlay profile (applies settings.json/MCP on top, keeps persona from --profile)",
    )
    init_p.add_argument("--dry-run", action="store_true", help="Print settings without writing")

    bundle_p = sub.add_parser("bundle", help="Manage the linked bundle (link, unlink, list, pull)")
    bundle_p.add_argument(
        "action",
        choices=["link", "unlink", "list", "pull"],
        help="link <path> | unlink | list | pull",
    )
    bundle_p.add_argument("bundle_path", nargs="?", default=None, help="Bundle directory path (for link)")
    bundle_p.add_argument("--rebase", action="store_true", help="Use --rebase when pulling")

    sub.add_parser("claude", help="Launch claude with profile flags (model, permission-mode, effort, etc.)")

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

    quota_p = sub.add_parser(
        "quota",
        help="Manage the Claude.ai console quota watcher",
    )
    quota_p.add_argument(
        "action",
        nargs="?",
        default="watch",
        choices=[
            "watch",
            "auth",
            "import-cookies",
            "status",
            "logs",
            "stop",
            "list",
            "switch",
            "restart",
            "remove",
            "dump-html",
        ],
        help="watch | auth | switch | list | restart | stop | status | logs | remove | import-cookies | dump-html",
    )
    quota_p.add_argument("quota_account", nargs="?", default=None, help="Account name (for auth, switch, remove)")
    quota_p.add_argument("--poll", type=int, default=60, help="Poll interval in seconds (default: 60)")

    daemon_p = sub.add_parser("daemon", help="Manage the sync daemon (auto-propagation)")
    daemon_p.add_argument(
        "action",
        nargs="?",
        default="start",
        choices=["start", "stop", "status", "logs"],
        help="start (default) — start background sync daemon; stop — kill daemon; status — show daemon state; logs — tail daemon log",
    )
    daemon_p.add_argument(
        "--poll",
        type=int,
        default=int(os.environ.get("AGENTIHOOKS_SYNC_POLL_SEC", "60")),
        help="Poll interval in seconds (default: 60, env: AGENTIHOOKS_SYNC_POLL_SEC)",
    )
    daemon_p.add_argument("--foreground", action="store_true", help="Run in foreground (for debugging)")

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
        source = getattr(args, "source", "") or "."
        root = Path(__file__).resolve().parent.parent
        cmd = ["uv", "tool", "install", "--editable", "--force", str(root)]
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
    elif args.command == "init":
        args.profile = getattr(args, "init_profile", None)
        args.settings_profile = getattr(args, "init_settings_profile", None) or ""
        if getattr(args, "local", False) and not args.repo:
            args.repo = "."
        cmd_init_unified(args)
    elif args.command == "settings-profile":
        _cmd_settings_profile(args)
    elif args.command == "ignore":
        cmd_ignore(Path(args.path).expanduser().resolve(), force=args.force)
    elif args.command == "quota":
        cmd_quota(args)
    elif args.command == "claude":
        # Pass everything after "claude" as extra args
        try:
            idx = sys.argv.index("claude")
            extra = sys.argv[idx + 1 :]
        except ValueError:
            extra = []
        cmd_claude(extra)
    elif args.command == "daemon":
        cmd_daemon(args)
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
    elif args.command == "prune":
        sys.path.insert(0, str(AGENTIHOOKS_ROOT))
        from scripts.sync_daemon import _get_valid_mcp_names, _prune_stale_mcp_servers

        known_servers_file = AGENTIHOOKS_STATE_DIR / "known-mcp-servers.json"
        valid = _get_valid_mcp_names()
        print(f"Valid MCP servers ({len(valid)}): {', '.join(sorted(valid))}")
        summary = _prune_stale_mcp_servers(known_servers_file, verbose=True)
        total = summary["pruned_disabled"] + summary["pruned_known"] + summary["pruned_settings"]
        if total == 0:
            print("No stale MCP entries found — everything is clean.")
        else:
            print(f"\nPruned {total} stale entries:")
            if summary["pruned_disabled"]:
                print(
                    f"  disabledMcpServers: {summary['pruned_disabled']} entries from {summary['projects_touched']} project(s)"
                )
            if summary["pruned_known"]:
                print(f"  known-mcp-servers.json: {summary['pruned_known']} entries")
            if summary["pruned_settings"]:
                print(f"  settings.local.json: {summary['pruned_settings']} entries")
    elif args.command == "migrate":
        cmd_migrate(args)
    elif args.command == "broadcast":
        _cmd_broadcast(args)


if __name__ == "__main__":
    main()
