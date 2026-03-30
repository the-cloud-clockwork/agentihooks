#!/usr/bin/env python3
# If accidentally run with bash, re-exec with python3 (polyglot trick).
""":'
exec python3 "$0" "$@"
exit
"""

# NOTE: The triple-quoted polyglot string above becomes __doc__ in Python,
# so the real usage text is stored in _USAGE_TEXT and used as the argparse epilog.
_USAGE_TEXT = """Examples:
    agentihooks global [--profile default]
        Install hooks, skills, agents, and CLAUDE.md into ~/.claude.
        --profile selects which profile's CLAUDE.md to link (default: 'default').
        Available profiles: agentihooks --list-profiles

    agentihooks project <path> [--profile default]
        Install a profile's .mcp.json into a target project directory.

    agentihooks uninstall [--yes]
        Remove all agentihooks artifacts installed by 'agentihooks global'.

    agentihooks mcp [list|install|uninstall|sync|add] [--dir PATH]
        Manage MCP server files from ~/.agentihooks/ (or --dir).
        Drop .json files with mcpServers into the directory, then:
          agentihooks mcp              # list available MCP files
          agentihooks mcp install      # pick one to install
          agentihooks mcp uninstall    # pick one to remove
          agentihooks mcp sync         # re-apply all installed files
          agentihooks mcp add <path>   # install a file directly by path

    agentihooks ignore [path] [--force]
        Create a .claudeignore in the current directory (or given path).
        Covers secrets, build artefacts, binaries, venvs, and IDE noise.
        --force overwrites an existing file.


    agentihooks connector [link|unlink|list|inspect]
        Manage external connectors (MCP/permissions adapters).
        Connectors are directories with connector.yml + per-profile permissions.
          agentihooks connector list                  # list linked connectors
          agentihooks connector link /path/to/dir     # link a connector
          agentihooks connector unlink <name>         # unlink by name
          agentihooks connector inspect /path/to/dir  # preview merge
          agentihooks connector new                   # interactive scaffold
          agentihooks connector new --name x --path . # headless scaffold
    agentihooks --loadenv [PATH] [-- COMMAND [ARGS...]]
        Load ~/.agentihooks/.env (or PATH) into the environment, then:
          - If COMMAND given: exec it with the loaded vars (use in aliases).
          - If no COMMAND: print 'export KEY=VALUE' lines for eval $().
        Examples:
          agentihooks --loadenv -- claude
          eval $(agentihooks --loadenv)

Re-run 'agentihooks global' after any changes to settings.base.json.
The script is idempotent.

Data directory: defaults to ~/.agentihooks/
  Override: export AGENTIHOOKS_HOME=/mnt/shared  (K8s / shared filesystem)
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


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge *override* into *base* (override wins)."""
    merged = deepcopy(base)
    for key, value in override.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = _deep_merge(merged[key], value)
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


def _register_target_global(profile: str) -> None:
    """Record the global install as a sync daemon target."""
    state = _load_state()
    targets = state.setdefault("targets", {})
    targets["global"] = {
        "path": str(CLAUDE_HOME),
        "profile": profile,
        "installed_at": datetime.now(timezone.utc).isoformat(),
    }
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


def cmd_bundle(action: str, path: str | None = None) -> None:
    """Handle 'agentihooks bundle' subcommands."""
    if action == "link":
        _bundle_link(Path(path).expanduser().resolve() if path else None)
    elif action == "unlink":
        _bundle_unlink()
    elif action == "list":
        _bundle_list()
    else:
        print(f"Unknown bundle action: {action}", file=sys.stderr)
        sys.exit(1)


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

    # Auto-discover and register connectors from bundle
    conn_dir = bundle_dir / "connectors"
    if conn_dir.is_dir():
        connectors = state.setdefault("connectors", {})
        for item in sorted(conn_dir.iterdir()):
            if item.is_dir() and (item / "connector.yml").exists():
                try:
                    meta = yaml.safe_load((item / "connector.yml").read_text())
                    conn_name = meta.get("name", item.name)
                    connectors[conn_name] = {
                        "path": str(item),
                        "linked_at": datetime.now(timezone.utc).isoformat(),
                        "source": "bundle",
                    }
                    print(f"  [OK] Auto-linked connector: {conn_name}")
                except Exception as exc:
                    print(f"  [WARN] Skipped {item.name}: {exc}")

    _save_state(state)

    # Summary
    profiles_dir = bundle_dir / "profiles"
    profile_names = sorted(p.name for p in profiles_dir.iterdir() if p.is_dir()) if profiles_dir.is_dir() else []

    print(f"[OK] Linked bundle: {bundle_dir}")
    if profile_names:
        print(f"     Profiles: {', '.join(profile_names)}")
    print()
    print("Run 'agentihooks global --profile <name>' to apply.")


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
        print(f"  [OK] Removed bundle connector: {name}")

    del state["bundle"]
    _save_state(state)
    print(f"[OK] Unlinked bundle: {bundle_path}")
    print()
    print("Run 'agentihooks global' to remove bundle settings.")


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
            print(f"  [WARN] Connector '{name}' at {conn_dir} missing connector.yml — skipping")
            continue
        if not conn_dir.is_dir():
            print(f"  [WARN] Connector '{name}' path {conn_dir} not found — skipping")
            continue

        try:
            meta = yaml.safe_load(conn_yml.read_text())
        except Exception as exc:
            print(f"  [WARN] Connector '{name}' connector.yml parse error: {exc} — skipping")
            continue

        # Base env (applied to all profiles)
        base_env = (meta.get("base") or {}).get("env", {})
        merged_env.update(base_env)

        # Profile-specific settings (fall back to "default" if exact profile missing)
        profile_dir = conn_dir / "profiles" / profile_name
        if not profile_dir.is_dir():
            fallback = conn_dir / "profiles" / "default"
            if fallback.is_dir():
                print(f"  [--] Connector '{name}': no profile '{profile_name}', falling back to 'default'")
                profile_dir = fallback
        if profile_dir.is_dir():
            perms_file = profile_dir / "permissions.json"
            if perms_file.exists():
                try:
                    perms = json.loads(perms_file.read_text())
                    merged_deny.extend(perms.get("deny", []))
                    merged_disabled_servers.extend(perms.get("disabledMcpjsonServers", []))
                except (json.JSONDecodeError, OSError) as exc:
                    print(f"  [WARN] Connector '{name}' permissions.json error: {exc}")

            env_file = profile_dir / "env.json"
            if env_file.exists():
                try:
                    env_data = json.loads(env_file.read_text())
                    merged_env.update(env_data)
                except (json.JSONDecodeError, OSError) as exc:
                    print(f"  [WARN] Connector '{name}' env.json error: {exc}")

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

    print(f"[OK] Linked connector '{conn_name}' v{version}")
    if desc:
        print(f"     {desc}")
    if profile_names:
        print(f"     Profiles: {', '.join(profile_names)}")
    print()
    print("Run 'agentihooks global' to apply connector rules to your settings.")


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
    print(f"[OK] Unlinked connector '{conn_name}'")
    print()
    print("Run 'agentihooks global' to remove connector rules from your settings.")


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

    print(f"[OK] Created connector at {conn_dir}")
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
    print("  3. agentihooks global --profile <name>")

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
        print(f"[OK] Created {config_path}")
    else:
        config = json.loads(config_path.read_text())
        if profile_arg:
            config["profile"] = profile_arg
        print(f"[OK] Read {config_path}")

    # Resolve and write .claude/settings.local.json
    _write_project_settings(repo_dir, config, dry_run=getattr(args, "dry_run", False))


def _write_project_settings(repo_dir: Path, config: dict, *, dry_run: bool = False) -> None:
    """Build and write .claude/settings.local.json from per-repo config."""
    profile_name = config.get("profile", "default")

    # Validate profile
    profile_dir = _resolve_profile_dir(profile_name)
    if profile_dir is None:
        print(f"  [WARN] Profile '{profile_name}' not found — using default")
        profile_dir = _resolve_profile_dir("default") or PROFILES_DIR / "default"
        profile_name = "default"

    # Load profile overrides
    overrides_path = profile_dir / "settings.overrides.json"
    profile_overrides = {}
    if overrides_path.exists():
        try:
            profile_overrides = load_json(overrides_path)
        except (json.JSONDecodeError, OSError):
            pass

    # Load connector rules for this profile
    conn_env, conn_deny, conn_disabled = _load_connectors(profile_name)

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
    # projects block, except those the profile whitelists.
    all_known = _get_all_known_mcp_names()
    profile_enabled = _get_profile_enabled_servers(profile_dir)
    to_disable = sorted(all_known - (profile_enabled or set()))
    if not dry_run:
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

    # Env — merge connector + repo + otel
    all_env = {}
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

    print(f"[OK] Wrote {out_path}")
    print(f"     Profile: {profile_name}")
    if all_disabled:
        print(f"     Disabled .mcp.json servers: {all_disabled}")
    if to_disable:
        print(f"     Blacklisted MCPs ({len(to_disable)}): {to_disable[:5]}{'...' if len(to_disable) > 5 else ''}")
    if all_deny:
        print(f"     Deny rules: {len(all_deny)}")
    if all_ask:
        print(f"     Ask rules: {len(all_ask)}")

    # Ensure .gitignore covers settings.local.json
    _ensure_local_settings_gitignored(repo_dir)


def _ensure_local_settings_gitignored(repo_dir: Path) -> None:
    """Ensure .claude/settings.local.json is gitignored."""
    entry = ".claude/settings.local.json"
    gitignore = repo_dir / ".gitignore"
    if gitignore.exists():
        content = gitignore.read_text()
        if entry not in content:
            with open(gitignore, "a") as f:
                f.write(f"\n{entry}\n")
            print(f"  [OK] Added {entry} to .gitignore")
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
        print(f"  [--] {_ENV_FILE_DST} already exists — not overwritten (your file)")
        return
    if _ENV_EXAMPLE_SRC.exists():
        shutil.copy2(_ENV_EXAMPLE_SRC, _ENV_FILE_DST)
        print(f"  [OK] Created {_ENV_FILE_DST}")
        print(f"       Configure your integrations: {_ENV_FILE_DST}")
    else:
        print(f"  [!!] .env.example not found — could not seed {_ENV_FILE_DST}")


# ---------------------------------------------------------------------------
# --loadenv: install agentihooksenv alias into ~/.bashrc (managed block)
# ---------------------------------------------------------------------------

_BASHRC = Path.home() / ".bashrc"
_BLOCK_START = "# === agentihooks ==="
_BLOCK_END = "# === end-agentihooks ==="


def _cmd_loadenv(env_file: Path, exec_cmd: list[str], *, force: bool = False) -> None:
    """Write a managed alias block into ~/.bashrc so `agentienv` sources the .env."""
    if not env_file.is_file():
        print(f"[!!] env file not found: {env_file}", file=sys.stderr)
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
        print(f"[OK] Updated agentihooks block in {_BASHRC}")
    else:
        # Append new block
        sep = "\n" if bashrc_text and not bashrc_text.endswith("\n") else ""
        _BASHRC.write_text(bashrc_text + sep + block, encoding="utf-8")
        print(f"[OK] Added agentihooks block to {_BASHRC}")

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
        print("  [!!] uv not found — skipping requirements install.")
        return

    for req in req_files:
        try:
            answer = input(f"Found {req} — install with uv? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\nSkipped.")
            return

        if answer != "y":
            print("  [--] Skipped.")
            continue

        if force:
            python = Path(sys.executable)
            print(f"  [..] Installing into system Python ({python}) ...")
        else:
            python = _detect_venv()
            if python is None:
                print("  [!!] No virtual environment found.")
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
            print(f"  [OK] Installed {req}")
        else:
            print(f"  [!!] uv pip install failed (exit {result.returncode})")


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
    """Print the currently installed global profile and exit."""
    claude_md = CLAUDE_HOME / _CLAUDE_MD_NAME
    if not claude_md.exists():
        print("not installed")
        return
    if not claude_md.is_symlink():
        print("unmanaged  (CLAUDE.md is not a symlink — installed manually)")
        return
    target = claude_md.resolve()
    # Check built-in profiles first, then bundle
    search_dirs = [PROFILES_DIR]
    bundle = _get_bundle_path()
    if bundle:
        search_dirs.append(bundle / "profiles")
    for search_dir in search_dirs:
        try:
            rel = target.relative_to(search_dir)
            profile_name = rel.parts[0]
            print(profile_name)
            return
        except ValueError:
            continue
    print(f"unknown  (symlink points outside profiles/: {target})")


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
        claude_md = profile_dir / _CLAUDE_SUBDIR / _CLAUDE_MD_NAME
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
    profile_name: str = args.profile

    # Validate profile exists (built-in or bundle)
    profile_dir = _resolve_profile_dir(profile_name)
    if profile_dir is None:
        available = _available_profiles()
        print(f"ERROR: Profile '{profile_name}' not found.", file=sys.stderr)
        print(f"Available profiles: {', '.join(available)}", file=sys.stderr)
        sys.exit(1)

    profile_source = "built-in" if (PROFILES_DIR / profile_name).is_dir() else "bundle"
    print(f"agentihooks root : {AGENTIHOOKS_ROOT}")
    print(f"Target           : {CLAUDE_HOME}")
    print(f"Profile source   : {profile_source}")
    print(f"Profile          : {profile_name}")
    # Resolve canonical Python: ~/.agentihooks/.venv wins over sys.executable
    # so that `agentihooks global` (run via uv tool from any project venv) always
    # bakes the right path into hook commands.
    _canonical_python = str(_detect_venv() or sys.executable)
    print(f"Python           : {_canonical_python}")
    print()

    # --- 1. Load and render base settings ---
    if not BASE_SETTINGS.exists():
        print(f"ERROR: {BASE_SETTINGS} not found.", file=sys.stderr)
        sys.exit(1)

    raw_settings = load_json(BASE_SETTINGS)
    rendered: dict = substitute_paths(raw_settings)  # NOSONAR — intentional object→dict cast
    rendered = substitute_paths(rendered, "__PYTHON__", _canonical_python)  # NOSONAR

    # --- 1b. Apply per-profile overrides (e.g. env vars like AGENTIHOOKS_SECRETS_MODE) ---
    overrides_path = profile_dir / "settings.overrides.json"
    if overrides_path.exists():
        overrides = load_json(overrides_path)
        rendered = _deep_merge(rendered, overrides)
        print(f"Applied profile overrides: {overrides_path}")

    # --- 1c. Apply linked connectors (additive: env vars + deny rules + disabled servers) ---
    conn_env, conn_deny, conn_disabled = _load_connectors(profile_name)
    if conn_env:
        rendered.setdefault("env", {}).update(conn_env)
        print(f"  [OK] Connector env: {list(conn_env.keys())}")
    if conn_deny:
        rendered.setdefault("permissions", {}).setdefault("deny", []).extend(conn_deny)
        print(f"  [OK] Connector deny rules: {len(conn_deny)}")
    if conn_disabled:
        existing_disabled = rendered.get("disabledMcpjsonServers", [])
        merged_disabled = list(dict.fromkeys(existing_disabled + conn_disabled))
        rendered["disabledMcpjsonServers"] = merged_disabled
        print(f"  [OK] Connector disabled MCP servers: {merged_disabled}")

    # --- 1d. OTEL baseline env vars from profile ---
    profile_yml_path = profile_dir / "profile.yml"
    if profile_yml_path.exists():
        import yaml

        profile_data = yaml.safe_load(profile_yml_path.read_text()) or {}
        otel_env = _build_otel_env(profile_data)
        if otel_env:
            rendered.setdefault("env", {}).update(otel_env)
            print(f"  [OK] OTEL env: {list(otel_env.keys())}")

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
    print(f"[OK] Wrote {existing_settings_path}")

    # --- 4. Symlink skills (directories only) ---
    _symlink_dir_contents(
        AGENTIHOOKS_ROOT / _CLAUDE_SUBDIR / "skills",
        CLAUDE_HOME / "skills",
        label="skill",
        filter_fn=lambda p: p.is_dir(),
    )

    # --- 5. Symlink agents (.md files only, excluding README.md) ---
    _symlink_dir_contents(
        AGENTIHOOKS_ROOT / _CLAUDE_SUBDIR / "agents",
        CLAUDE_HOME / "agents",
        label="agent",
        filter_fn=lambda p: p.suffix == ".md" and p.name != "README.md",
    )

    # --- 6. Symlink commands (.md files only, excluding README.md) ---
    _symlink_dir_contents(
        AGENTIHOOKS_ROOT / _CLAUDE_SUBDIR / "commands",
        CLAUDE_HOME / "commands",
        label="command",
        filter_fn=lambda p: p.suffix == ".md" and p.name != "README.md",
    )

    # --- 7. Symlink CLAUDE.md from the chosen profile ---
    _resolved_profile_dir = _resolve_profile_dir(profile_name) or PROFILES_DIR / profile_name
    profile_claude_md = _resolved_profile_dir / _CLAUDE_SUBDIR / _CLAUDE_MD_NAME
    claude_md_dst = CLAUDE_HOME / _CLAUDE_MD_NAME
    _install_claude_md(profile_claude_md, claude_md_dst, profile_name)

    # --- 8. Install profile MCP servers to user scope (~/.claude.json) ---
    _install_user_mcp(profile_name)

    # --- 9. Re-apply any custom MCPs tracked in state.json ---
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
            print(f"  [--] AGENTIHOOKS_MCP_FILE={mcp_file_env} not found — skipping.")

    # --- 9c. Blacklist all MCPs across all projects ---
    print()
    print("Applying MCP blacklist to all projects...")
    _blacklist_all_projects_mcps(profile_dir)

    # --- 10. Install agentihooks CLI tool to ~/.local/bin ---
    print()
    _install_cli_tool()

    # --- 11. Seed ~/.agentihooks/.env from .env.example (first run only) ---
    print()
    _seed_user_env_file()

    # --- 12. Register as sync daemon target ---
    _register_target_global(profile_name)

    # --- Done ---
    print()
    print("Installation complete.")
    print()
    print("Verification steps:")
    print(f"  ls -la {existing_settings_path}")
    print(f"  ls -la {claude_md_dst}")
    print("  Open Claude Code in any project → run /status (hooks should be active)")
    print("  Run /skills to list installed skills")
    print()
    print("To update after settings.base.json changes:")
    print("  agentihooks global")
    print()
    print("Shell tip: wrap Claude Code to load MCP keys from ~/.agentihooks/.env:")
    print("  alias cc='agentihooks --loadenv -- claude'")
    print("  Or for eval mode: eval $(agentihooks --loadenv)")


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
    except (OSError, Exception):
        return None


def _write_project_disabled_mcps(repo_path: Path, disabled_names: list[str]) -> None:
    """Write disabledMcpServers to ~/.claude.json projects[path] block."""
    if not _CLAUDE_JSON.exists():
        return
    try:
        data = load_json(_CLAUDE_JSON)
        projects = data.setdefault("projects", {})
        proj = projects.setdefault(str(repo_path), {})
        proj["disabledMcpServers"] = disabled_names
        save_json(_CLAUDE_JSON, data)
        print(f"  [OK] Wrote disabledMcpServers to ~/.claude.json ({len(disabled_names)} servers)")
    except (json.JSONDecodeError, OSError) as e:
        print(f"  [WARN] Could not update ~/.claude.json: {e}")



def _blacklist_all_projects_mcps(profile_dir: Path) -> None:
    """Blacklist all known MCPs in every project entry in ~/.claude.json.

    Called by 'agentihooks global' to ensure every project starts with all
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

    profile_enabled = _get_profile_enabled_servers(profile_dir)
    to_disable = sorted(all_known - (profile_enabled or set()))

    updated = 0
    for proj_path, proj_data in projects.items():
        if not isinstance(proj_data, dict):
            continue
        proj_data["disabledMcpServers"] = to_disable
        updated += 1

    if updated:
        save_json(_CLAUDE_JSON, data)
        print(f"  [OK] Blacklisted {len(to_disable)} MCPs across {updated} project(s) in ~/.claude.json")


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
        print(f"  [OK] Added user-scope MCP servers  : {', '.join(added)}")
    if updated:
        print(f"  [OK] Updated user-scope MCP servers: {', '.join(updated)}")
    if not added and not updated:
        print(f"  [--] User-scope MCP servers unchanged: {', '.join(servers.keys())}")


def _remove_mcp_from_user_scope(servers: dict) -> None:
    """Remove *servers* keys from the top-level mcpServers of ~/.claude.json."""
    if not _CLAUDE_JSON.exists():
        print("  [--] ~/.claude.json does not exist — nothing to remove.")
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
        print(f"  [OK] Removed user-scope MCP servers: {', '.join(removed)}")
    if missing:
        print(f"  [--] Not found (already removed?)  : {', '.join(missing)}")


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
        print(f"  [--] No mcpServers found in {mcp_path} — nothing to do.")
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
        print(f"  [--] No MCP files tracked in {STATE_JSON} — nothing to sync.")
        return

    print(f"Syncing {len(paths)} tracked MCP file(s) from {STATE_JSON}:")
    for path_str in paths:
        p = Path(path_str)
        if not p.exists():
            print(f"  [!!] Skipping missing file: {path_str}")
            continue
        try:
            raw = load_json(p)
        except (json.JSONDecodeError, OSError) as exc:
            print(f"  [!!] Cannot read {path_str}: {exc}")
            continue
        servers: dict = raw.get("mcpServers", {})
        if not servers:
            print(f"  [--] No mcpServers in {path_str} — skipping.")
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
        print(f"[!!] Not a directory: {lib_path}", file=sys.stderr)
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
        print("  [!!] uv not found — install uv first: https://docs.astral.sh/uv/getting-started/installation/")
        print("       Then re-run: uv run agentihooks global")
        return

    result = subprocess.run(
        [uv, "tool", "install", "--editable", "--force", "."],
        cwd=AGENTIHOOKS_ROOT,
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        print("  [OK] CLI installed via: uv tool install --editable .")
    else:
        print(f"  [!!] uv tool install failed: {result.stderr.strip()}")


def _uninstall_cli_tool() -> None:
    """Uninstall the agentihooks CLI via ``uv tool uninstall``."""
    import subprocess

    uv = shutil.which("uv")
    if not uv:
        print("  [!!] uv not found — cannot uninstall CLI automatically.")
        print(f"       Remove manually: uv tool uninstall {_CLI_NAME}")
        return

    result = subprocess.run(
        [uv, "tool", "uninstall", _CLI_NAME],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        print(f"  [OK] Uninstalled CLI via: uv tool uninstall {_CLI_NAME}")
    else:
        stderr = result.stderr.strip()
        if "not installed" in stderr.lower():
            print(f"  [--] {_CLI_NAME} was not installed via uv tool (skipping)")
        else:
            print(f"  [!!] uv tool uninstall failed: {stderr}")


# ---------------------------------------------------------------------------
# Symlink helpers
# ---------------------------------------------------------------------------


def _remove_agentihooks_symlinks(dst_dir: Path, label: str) -> int:
    """Remove symlinks in *dst_dir* whose resolved target is inside AGENTIHOOKS_ROOT.

    Returns the count of removed links. Non-symlinks and symlinks pointing
    elsewhere are left untouched (user-created links stay safe).
    """
    if not dst_dir.exists():
        return 0
    count = 0
    root_str = str(AGENTIHOOKS_ROOT)
    for link in sorted(dst_dir.iterdir()):
        if not link.is_symlink():
            continue
        try:
            target = link.resolve()
        except OSError:
            continue
        if str(target).startswith(root_str):
            link.unlink()
            print(f"  [RM] Removed {label} symlink: {link.name}")
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
            print(f"  [RM] Removed broken symlink: {link.name}")
        elif target.parent.resolve() == src_dir.resolve() and filter_fn and not filter_fn(target):
            link.unlink()
            print(f"  [RM] Removed stale symlink: {link.name}")


def _link_item(item: Path, link: Path, label: str) -> None:
    """Create or update a single symlink *link* → *item*."""
    if link.is_symlink():
        if link.resolve() == item.resolve():
            print(f"  [--] {label} '{item.name}' already linked → {item}")
        else:
            link.unlink()
            link.symlink_to(item)
            print(f"  [OK] Re-linked {label} '{item.name}' → {item}")
    elif link.exists():
        print(f"  [!!] {label} '{item.name}' exists at {link} and is not a symlink – skipping (remove manually)")
    else:
        link.symlink_to(item)
        print(f"  [OK] Linked {label} '{item.name}' → {item}")


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


def _install_claude_md(src: Path, dst: Path, profile_name: str) -> None:
    """Symlink *dst* (~/.claude/CLAUDE.md) → *src* (profile CLAUDE.md)."""
    if not src.exists():
        print(f"  [!!] Profile {_CLAUDE_MD_NAME} not found at {src} — skipping.")
        print(f"       Available profiles: {_available_profiles()}")
        return

    if dst.is_symlink():
        if dst.resolve() == src.resolve():
            print(f"  [--] {_CLAUDE_MD_NAME} already linked → {src}")
        else:
            dst.unlink()
            dst.symlink_to(src)
            print(f"  [OK] Re-linked {_CLAUDE_MD_NAME} → {src}")
        return

    if dst.exists():
        print(f"\nA {_CLAUDE_MD_NAME} already exists at {dst}.")
        answer = (
            input(f"Replace with symlink to profiles/{profile_name}/{_CLAUDE_SUBDIR}/{_CLAUDE_MD_NAME}? [y/N] ")
            .strip()
            .lower()
        )
        if answer == "y":
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup = dst.with_suffix(f".md.bak.{timestamp}")
            shutil.copy2(dst, backup)
            print(f"  Backed up existing {_CLAUDE_MD_NAME} → {backup}")
            dst.unlink()
            dst.symlink_to(src)
            print(f"  [OK] Linked {_CLAUDE_MD_NAME} → {src}")
        else:
            print(f"  [--] Skipped {_CLAUDE_MD_NAME} linking.")
        return

    dst.symlink_to(src)
    print(f"  [OK] Linked {_CLAUDE_MD_NAME} → {src}")


# ---------------------------------------------------------------------------
# Uninstall
# ---------------------------------------------------------------------------


def _collect_all_managed_mcp_servers() -> dict:
    """Return the union of all MCP servers managed by agentihooks.

    Collects servers from:
    1. The hooks-utils server (generated from profile mcp_categories)
    2. All files tracked in ~/.agentihooks/state.json mcpFiles

    Returns a merged {name: config} dict.
    """
    merged: dict = {}

    # --- hooks-utils server (always present, categories don't matter for uninstall) ---
    mcp_config = _build_mcp_config("all")
    merged.update(mcp_config["mcpServers"])

    # --- State-tracked MCP files ---
    state = _load_state()
    for path_str in state.get("mcpFiles", []):
        p = Path(path_str)
        if not p.exists():
            continue
        try:
            raw = load_json(p)
            for name, cfg in raw.get("mcpServers", {}).items():
                merged[name] = cfg
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
    claude_md_dst = CLAUDE_HOME / _CLAUDE_MD_NAME

    # --- Audit ---
    remove_settings = False
    if settings_path.exists():
        try:
            remove_settings = load_json(settings_path).get(MANAGED_BY_KEY) == MANAGED_BY_VALUE
        except (json.JSONDecodeError, OSError):
            pass

    def _count_agentihooks_symlinks(d: Path) -> int:
        if not d.exists():
            return 0
        root_str = str(AGENTIHOOKS_ROOT)
        return sum(1 for lnk in d.iterdir() if lnk.is_symlink() and str(lnk.resolve()).startswith(root_str))

    n_skills = _count_agentihooks_symlinks(skills_dir)
    n_agents = _count_agentihooks_symlinks(agents_dir)
    n_commands = _count_agentihooks_symlinks(commands_dir)

    remove_claude_md = claude_md_dst.is_symlink() and str(claude_md_dst.resolve()).startswith(str(PROFILES_DIR))

    managed_servers = _collect_all_managed_mcp_servers()

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
    if remove_claude_md:
        print(f"  {claude_md_dst}  (symlink → profiles/)")
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
        print(f"[OK] Removed {settings_path}")
    else:
        print(f"[--] Skipped {settings_path} (not managed)")

    # --- 4. Remove symlinks in skills, agents, commands ---
    print()
    for dst_dir, label in [
        (skills_dir, "skill"),
        (agents_dir, "agent"),
        (commands_dir, "command"),
    ]:
        n = _remove_agentihooks_symlinks(dst_dir, label)
        if n == 0:
            print(f"  [--] No {label} symlinks found in {dst_dir}")

    # --- 5. Remove CLAUDE.md symlink ---
    print()
    if remove_claude_md:
        claude_md_dst.unlink()
        print(f"[OK] Removed {claude_md_dst}")
    else:
        print(f"[--] Skipped {claude_md_dst} (not a managed symlink)")

    # --- 6. Remove MCP servers from ~/.claude.json ---
    print()
    if managed_servers:
        print(f"Removing {len(managed_servers)} MCP server(s) from {_CLAUDE_JSON}:")
        _remove_mcp_from_user_scope(managed_servers)
    else:
        print(f"  [--] No managed MCP servers to remove from {_CLAUDE_JSON}")

    # --- 7. Uninstall CLI ---
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
        answer = input(f"{_MCP_JSON_NAME} already exists at {mcp_dst}. Overwrite? [y/N] ").strip().lower()
        if answer != "y":
            print("Aborted.")
            sys.exit(0)

    save_json(mcp_dst, rendered_mcp)
    print(f"[OK] Wrote {mcp_dst}")
    print()
    print(f"Next: open Claude Code in '{project_path}' and run /mcp to verify the hooks-utils server.")

    # Register as sync daemon target
    _register_target_project(project_path, profile_name)


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
            print(f"  [!!] Cannot read servers from {selected_file} — removing from tracking.")
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
        print(f"  [--] {dest} already exists — use --force to overwrite")
        return

    action = "Overwrote" if dest.exists() else "Created"
    dest.write_text(CLAUDEIGNORE_TEMPLATE, encoding="utf-8")
    print(f"  [OK] {action} {dest}")
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
        if pid_file.exists():
            try:
                pid = int(pid_file.read_text().strip())
                os.kill(pid, signal.SIGTERM)
                pid_file.unlink(missing_ok=True)
                print(f"[sync] Daemon stopped (PID {pid}).")
            except (ProcessLookupError, ValueError):
                pid_file.unlink(missing_ok=True)
                print("[sync] No daemon running (stale PID cleaned up).")
        else:
            print("[sync] No daemon running.")

    elif args.action == "status":
        state = _load_state()
        targets = state.get("targets", {})

        # PID status
        running = False
        if pid_file.exists():
            try:
                pid = int(pid_file.read_text().strip())
                os.kill(pid, 0)
                running = True
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
            print("  Global target: not registered (run 'agentihooks global' first)")
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


def cmd_quota(args: "argparse.Namespace") -> None:
    """Launch or interact with the Claude.ai quota watcher."""
    watcher = AGENTIHOOKS_ROOT / "scripts" / "claude_usage_watcher.py"
    if not watcher.exists():
        print(f"ERROR: quota watcher not found at {watcher}", file=sys.stderr)
        sys.exit(1)

    python = str(_detect_venv() or sys.executable)

    if args.action == "auth":
        os.execv(python, [python, str(watcher), "--auth"])

    elif args.action == "dump-html":
        os.execv(python, [python, str(watcher), "--dump-html"])

    elif args.action == "import-cookies":
        os.execv(python, [python, str(watcher), "--import-cookies"])

    elif args.action == "logs":
        log_file = Path.home() / ".agentihooks" / "logs" / "quota-watcher.log"
        if not log_file.exists():
            print("No log file yet. Start the daemon first:  agentihooks quota")
            sys.exit(0)
        os.execlp("tail", "tail", "-f", str(log_file))

    elif args.action == "stop":
        pid_file = Path.home() / ".agentihooks" / "quota-watcher.pid"
        if pid_file.exists():
            import signal

            try:
                pid = int(pid_file.read_text().strip())
                os.kill(pid, signal.SIGTERM)
                pid_file.unlink(missing_ok=True)
                print(f"[quota] Daemon stopped (PID {pid}).")
            except (ProcessLookupError, ValueError):
                pid_file.unlink(missing_ok=True)
                print("[quota] No daemon running.")
        else:
            print("[quota] No daemon running.")

    elif args.action == "status":
        import json as _json

        usage_file = Path(os.getenv("CLAUDE_USAGE_FILE", str(Path.home() / ".agentihooks" / "claude_usage.json")))
        if not usage_file.exists():
            print("No quota data yet. Run:  agentihooks quota")
            sys.exit(0)
        data = _json.loads(usage_file.read_text())
        print(_json.dumps(data, indent=2))

    else:  # watch (default) — always headless
        cmd = [python, str(watcher)]
        if args.poll != 60:
            cmd += ["--poll", str(args.poll)]
        os.execv(python, cmd)


def main() -> None:
    # Split argv on '--' so everything after it becomes the exec command.
    _argv = sys.argv[1:]
    _sep = _argv.index("--") if "--" in _argv else None
    _exec_cmd: list[str] = _argv[_sep + 1 :] if _sep is not None else []
    _argv = _argv[:_sep] if _sep is not None else _argv

    parser = argparse.ArgumentParser(
        description="Install agentihooks settings/hooks/skills/agents to ~/.claude or a project.",
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
        "--loadenv",
        nargs="?",
        const="",
        metavar="PATH",
        help="Install agentienv alias into ~/.bashrc and offer to install requirements.txt",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="With --loadenv: install requirements into system Python (for Docker/CI, skips venv check)",
    )
    sub = parser.add_subparsers(dest="command")

    glob_p = sub.add_parser("global", help="Install hooks + skills + agents into ~/.claude")
    glob_p.add_argument(
        "--profile",
        default=os.environ.get("AGENTIHOOKS_PROFILE", "default"),
        help=f"Profile whose CLAUDE.md to link (default: 'default', env: AGENTIHOOKS_PROFILE). Available: {', '.join(_available_profiles())}",
    )

    proj = sub.add_parser("project", help="Install a profile's .mcp.json into a target project")
    proj.add_argument("path", help="Target project directory")
    proj.add_argument(
        "--profile",
        default=os.environ.get("AGENTIHOOKS_PROFILE", "default"),
        help="Profile to use (default: 'default', env: AGENTIHOOKS_PROFILE)",
    )

    unsub = sub.add_parser("uninstall", help="Remove all agentihooks artifacts from the system")
    unsub.add_argument("--yes", "-y", action="store_true", help="Skip confirmation prompt")

    mcp_p = sub.add_parser(
        "mcp",
        help="Manage MCP server files (list, install, uninstall, sync, add)",
    )
    mcp_p.add_argument(
        "action",
        nargs="?",
        default="list",
        choices=["list", "install", "uninstall", "sync", "add"],
        help="Action to perform (default: list)",
    )
    mcp_p.add_argument(
        "mcp_path",
        nargs="?",
        default=None,
        help="Path to MCP file (only used with 'add' action)",
    )
    mcp_p.add_argument(
        "--dir",
        default=None,
        help=f"Directory to scan for MCP files (default: {AGENTIHOOKS_STATE_DIR})",
    )

    init_p = sub.add_parser(
        "init", help="Set up per-repo agentihooks config (.agentihooks.json → .claude/settings.local.json)"
    )
    init_p.add_argument("--repo", default=None, help="Target repo directory (default: cwd)")
    init_p.add_argument("--profile", dest="init_profile", default=None, help="Profile to use (headless mode)")
    init_p.add_argument("--dry-run", action="store_true", help="Print settings without writing")
    bundle_p = sub.add_parser("bundle", help="Manage external bundle (profiles + connectors)")
    bundle_sub = bundle_p.add_subparsers(dest="bundle_action")
    bundle_link = bundle_sub.add_parser("link", help="Link a bundle directory")
    bundle_link.add_argument("bundle_path", help="Path to bundle directory")
    bundle_sub.add_parser("unlink", help="Unlink the current bundle")
    bundle_sub.add_parser("list", help="Show linked bundle contents")
    conn_p = sub.add_parser("connector", help="Manage external connectors (MCP/permissions adapters)")
    conn_sub = conn_p.add_subparsers(dest="connector_action")
    conn_link = conn_sub.add_parser("link", help="Link a connector directory")
    conn_link.add_argument("connector_path", help="Path to connector directory")
    conn_unlink = conn_sub.add_parser("unlink", help="Unlink a connector by name")
    conn_unlink.add_argument("connector_name", help="Connector name")
    conn_sub.add_parser("list", help="List linked connectors")
    conn_inspect = conn_sub.add_parser("inspect", help="Preview what a connector would merge")
    conn_inspect.add_argument("connector_path", help="Path to connector directory")
    conn_new = conn_sub.add_parser("new", help="Create a new connector scaffold (interactive or headless)")
    conn_new.add_argument("--name", dest="new_name", help="Connector name (headless mode)")
    conn_new.add_argument("--path", dest="new_path", help="Parent directory for the connector (headless mode)")
    conn_new.add_argument("--description", dest="new_description", help="Connector description (headless mode)")
    conn_new.add_argument("--profiles", dest="new_profiles", help="Comma-separated profile names (headless mode)")
    conn_new.add_argument("--base-env", dest="new_base_env", help="Base env vars as KEY=VAL,KEY2=VAL2 (headless mode)")
    conn_new.add_argument("--link", dest="new_auto_link", action="store_true", help="Auto-link after creation")
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
        choices=["watch", "auth", "import-cookies", "status", "logs", "stop"],
        help="watch (default) — start background daemon; auth — open browser + paste cookie; import-cookies — paste only; status — print quota; logs — tail daemon log; stop — kill daemon; dump-html — dump usage page HTML for scraper debugging",
    )
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

    args = parser.parse_args(_argv)

    if args.loadenv is not None:
        _cmd_loadenv(
            Path(args.loadenv).expanduser() if args.loadenv else _ENV_FILE_DST,
            _exec_cmd,
            force=args.force,
        )
        return

    if args.list_profiles:
        list_profiles()
        return

    if args.query:
        query_active_profile()
        return

    if not args.command:
        parser.print_help()
        sys.exit(1)

    if args.command == "global":
        install_global(args)
    elif args.command == "project":
        install_project(args)
    elif args.command == "uninstall":
        uninstall_global(args)
    elif args.command == "mcp":
        scan_dir = Path(args.dir).expanduser().resolve() if args.dir else None
        mcp_path = Path(args.mcp_path).expanduser().resolve() if args.mcp_path else None
        cmd_mcp_action(args.action, scan_dir, mcp_path=mcp_path)
    elif args.command == "ignore":
        cmd_ignore(Path(args.path).expanduser().resolve(), force=args.force)
    elif args.command == "quota":
        cmd_quota(args)
    elif args.command == "init":
        # Map init_profile to profile for cmd_init
        args.profile = getattr(args, "init_profile", None)
        cmd_init(args)
    elif args.command == "bundle":
        action = getattr(args, "bundle_action", None) or "list"
        bundle_path = getattr(args, "bundle_path", None)
        cmd_bundle(action, path=bundle_path)
    elif args.command == "connector":
        action = getattr(args, "connector_action", None) or "list"
        conn_path = getattr(args, "connector_path", None) or getattr(args, "new_path", None)
        conn_name = getattr(args, "connector_name", None) or getattr(args, "new_name", None)
        cmd_connector(
            action,
            path=conn_path,
            name=conn_name,
            description=getattr(args, "new_description", None),
            profiles=getattr(args, "new_profiles", None),
            base_env=getattr(args, "new_base_env", None),
            auto_link=getattr(args, "new_auto_link", False),
        )
    elif args.command == "daemon":
        cmd_daemon(args)


if __name__ == "__main__":
    main()
