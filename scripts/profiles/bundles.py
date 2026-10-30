from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from hooks.context import conditions, profile_chain
from scripts.targets._common import _install_module

_SLOTS = {
    "root": ("bundle", "bundle"),
    "shared": ("shared_bundle", "shared bundle"),
    "user": ("user_bundle", "user bundle"),
}
_WINNERS = {"root": "the root's", "shared": "the shared bundle's"}


def _slot(user: bool = False, shared: bool = False) -> str:
    return "user" if user else "shared" if shared else "root"


def _names(user: bool, shared: bool = False) -> tuple[str, str]:
    return _SLOTS[_slot(user, shared)]


def _holders(kinds: list[str]) -> str:
    named = [f"the {kind}" for kind in kinds]
    return f"{', '.join(named[:-1])} and {named[-1]}" if len(named) > 2 else " and ".join(named)


def warn_unavailable() -> None:
    _i = _install_module()
    for kind, path in profile_chain.missing_bundle_roots(_i._load_state()):
        _i._cprint(f"  [WARN] Configured {kind} bundle {path} is missing - not applied.")
    roots = (_i._get_bundle_path(), _i._get_user_bundle_path(), _i._get_shared_bundle_path())
    holders = profile_chain.profile_holders(*roots)
    for name in profile_chain.profile_collisions(*roots):
        kinds = holders[name]
        _i._cprint(f"  [WARN] Profile '{name}' exists in {_holders(kinds)} bundle - {_WINNERS[kinds[0]]} is used.")


def link_user_arg(path: str | None) -> None:
    if not path:
        return
    user_dir = Path(path).expanduser().resolve()
    if not user_dir.is_dir():
        print(f"ERROR: User bundle directory not found: {user_dir}", file=sys.stderr)
        sys.exit(1)
    link(user_dir, user=True)
    print()


def cmd_bundle(
    action: str, path: str | None = None, rebase: bool = False, user: bool = False, shared: bool = False
) -> None:
    """Handle 'agentihooks bundle' subcommands."""
    if action == "link":
        link(Path(path).expanduser().resolve() if path else None, user=user, shared=shared)
    elif action == "unlink":
        unlink(user=user, shared=shared)
    elif action == "list":
        list_bundle(user=user, shared=shared)
    elif action == "pull":
        pull(rebase=rebase, user=user, shared=shared)
    else:
        print(f"Unknown bundle action: {action}", file=sys.stderr)
        sys.exit(1)


def _linked(user: bool = False, shared: bool = False) -> Path | None:
    _i = _install_module()
    getters = {"root": _i._get_bundle_path, "shared": _i._get_shared_bundle_path, "user": _i._get_user_bundle_path}
    return getters[_slot(user, shared)]()


def pull(rebase: bool = False, user: bool = False, shared: bool = False) -> None:
    """Git pull the linked bundle directory."""
    _i = _install_module()
    label = _names(user, shared)[1]
    bundle_dir = _linked(user, shared)
    if not bundle_dir:
        print(f"No {label} linked.", file=sys.stderr)
        sys.exit(1)

    if not (bundle_dir / ".git").exists():
        print(f"ERROR: {bundle_dir} is not a git repository.", file=sys.stderr)
        sys.exit(1)

    cmd = ["git", "-C", str(bundle_dir), "pull"]
    if rebase:
        cmd.append("--rebase")

    print(f"Pulling bundle: {bundle_dir}")
    print(f"  $ {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.stdout.strip():
        print(result.stdout.strip())
    if result.stderr.strip():
        print(result.stderr.strip())

    if result.returncode != 0:
        print("ERROR: git pull failed.", file=sys.stderr)
        sys.exit(1)

    _i._cprint("[OK] Bundle updated.")


def link(bundle_dir: Path | None, user: bool = False, shared: bool = False) -> None:
    """Link a bundle directory as the root bundle, or as the shared or user bundle beside it."""
    _i = _install_module()
    if bundle_dir is None:
        print("ERROR: Provide the path to a bundle directory.", file=sys.stderr)
        sys.exit(1)

    if not bundle_dir.is_dir():
        print(f"ERROR: {bundle_dir} is not a directory.", file=sys.stderr)
        sys.exit(1)

    key, label = _names(user, shared)
    state = _i._load_state()
    for held, (other_key, _) in _SLOTS.items():
        other = (state.get(other_key) or {}).get("path") if other_key != key else None
        if other and Path(other).expanduser().resolve() == bundle_dir.resolve():
            print(f"ERROR: {bundle_dir} is already linked as the {held} bundle.", file=sys.stderr)
            sys.exit(1)
    old_bundle = state.get(key)
    if old_bundle:
        print(f"Replacing existing {label}: {old_bundle['path']}")

    state[key] = {
        "path": str(bundle_dir),
        "linked_at": datetime.now(timezone.utc).isoformat(),
    }
    if not user and not shared:
        state[key]["owner"] = conditions._remote_owner(bundle_dir)

    _i._save_state(state)

    profiles_dir = bundle_dir / "profiles"
    profile_names = sorted(p.name for p in profiles_dir.iterdir() if p.is_dir()) if profiles_dir.is_dir() else []
    has_claude = (bundle_dir / ".claude").is_dir()

    _i._cprint(f"[OK] Linked {label}: {bundle_dir}")
    if profile_names:
        print(f"     Profiles: {', '.join(profile_names)}")
    if has_claude:
        print("     Global .claude/: skills, commands, agents")
    print()
    print("Run 'agentihooks init' to apply.")


def _generated(path: Path, entry: dict) -> bool:
    if path.is_symlink():
        return os.readlink(path).rstrip("/") == str(entry.get("target", "")).rstrip("/")
    return entry.get("kind") == "rules" and path.is_file()


def _sweep_generated(root: Path, label: str = "user bundle") -> None:
    _i = _install_module()
    swept = []
    for link, entry in _i._state_links().items():
        path = Path(link)
        if _generated(path, entry) and Path(entry.get("target", "")).is_relative_to(root):
            path.unlink()
            swept.append(path)
            _i._cprint(f"  [RM] Removed {label} link: {path}")
    _i._state_forget_links(swept)


def unlink(user: bool = False, shared: bool = False) -> None:
    """Unlink the root bundle, or only the shared or user bundle."""
    _i = _install_module()
    key, label = _names(user, shared)
    state = _i._load_state()
    bundle = state.get(key)
    if not bundle:
        print(f"No {label} linked.")
        return

    bundle_path = bundle["path"]

    connectors = state.get("connectors", {})
    to_remove = [] if user or shared else [name for name, info in connectors.items() if info.get("source") == "bundle"]
    for name in to_remove:
        del connectors[name]
        _i._cprint(f"  [OK] Removed bundle connector: {name}")

    del state[key]
    _i._save_state(state)
    if user or shared:
        _sweep_generated(Path(bundle_path), label)
    _i._cprint(f"[OK] Unlinked {label}: {bundle_path}")
    print()
    print("Run 'agentihooks init' to remove bundle settings.")


def list_bundle(user: bool = False, shared: bool = False) -> None:
    """Show the linked bundle and its contents."""
    _i = _install_module()
    key, label = _names(user, shared)
    bundle = _linked(user, shared)
    if not bundle:
        print(f"No {label} linked.")
        print()
        print("Link one with:")
        flag = "--user " if user else "--shared " if shared else ""
        print(f"  agentihooks bundle link {flag}/path/to/.agentihooks")
        return

    linked_at = _i._load_state().get(key, {}).get("linked_at", "?")

    print(f"{label.capitalize()}: {bundle}")
    print(f"Linked: {linked_at}")
    print()

    profiles_dir = bundle / "profiles"
    if profiles_dir.is_dir():
        profile_names = sorted(p.name for p in profiles_dir.iterdir() if p.is_dir())
        if profile_names:
            print(f"Profiles ({len(profile_names)}):")
            for name in profile_names:
                print(f"  {name}")
            print()

    conn_dir = bundle / "connectors"
    if conn_dir.is_dir():
        conn_names = sorted(d.name for d in conn_dir.iterdir() if d.is_dir() and (d / "connector.yml").exists())
        if conn_names:
            print(f"Connectors ({len(conn_names)}):")
            for name in conn_names:
                print(f"  {name}")
            print()


def user_last(argv: list[str]) -> list[str]:
    flags = [flag for flag in ("--shared", "--user") if flag in argv]
    if argv[:1] != ["bundle"] or not flags:
        return argv
    return [arg for arg in argv if arg not in flags] + flags


def add_parser(sub) -> None:
    bundle_p = sub.add_parser("bundle", help="Manage the linked bundle (new, link, unlink, list, pull)")
    bundle_p.add_argument(
        "action",
        choices=["new", "link", "unlink", "list", "pull"],
        help="new <path> | link <path> | unlink | list | pull",
    )
    bundle_p.add_argument("bundle_path", nargs="?", default=None, help="Bundle directory path (for link)")
    bundle_p.add_argument("--rebase", action="store_true", help="Use --rebase when pulling")
    scope = bundle_p.add_mutually_exclusive_group()
    scope.add_argument("--user", action="store_true", help="Act on the personal user bundle, not the root bundle")
    scope.add_argument(
        "--shared", action="store_true", help="Act on the shared bundle between the root and the user bundle"
    )
