import argparse
import json
import subprocess
import sys
from pathlib import Path

import yaml

from hooks.context.profile_chain import PACKAGE_ROLES
from scripts import install
from scripts.profiles.render import KEY_SEPARATOR

BUNDLE_FOLDERS = ("skills", "agents", "commands", "rules", "conditions")
OVERLAY_FOLDERS = ("skills", "rules")
BUNDLE_README = """# Bundle

Your agentihooks profiles, overlays, skills, rules and MCP servers. Layout: the agentihooks Bundles docs page.

- `agentihooks overlay new NAME --wears engineer` adds an overlay under `profiles/`.
- `agentihooks overlay check NAME` validates it.
- Commit it: a swarm renders overlays from the bundle at a pinned commit.
- `agentihooks init` applies the bundle.
"""


def base_roles() -> list[str]:
    return sorted(p.name for p in PACKAGE_ROLES.iterdir() if p.is_dir() and not p.name.startswith("_"))


def _keep(folder: Path) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / ".gitkeep").write_text("")


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")


def _fail(message: str) -> int:
    print(f"ERROR: {message}", file=sys.stderr)
    return 1


def bundle_new(target: Path) -> int:
    if target.exists() and not target.is_dir():
        return _fail(f"{target} is a file; pick a new folder for the bundle")
    if target.exists() and any(target.iterdir()):
        return _fail(f"{target} is not empty; pick a new folder for the bundle")
    target.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(target)], check=True)
    _write_json(target / "enforcements.json", {"enforcements": []})
    _write_json(target / ".claude" / ".mcp.json", {"mcpServers": {}})
    for folder in BUNDLE_FOLDERS:
        _keep(target / ".claude" / folder)
    _keep(target / "profiles")
    (target / "README.md").write_text(BUNDLE_README)
    install._bundle_link(target)
    return 0


def _role_problems(roles: object) -> list[str]:
    if not isinstance(roles, list):
        return ["wears must be a list of base roles"]
    if not roles:
        return ["wears no base role"]
    known = base_roles()
    return [f"{role} is not a base role; pick from {', '.join(known)}" for role in roles if role not in known]


def _name_problems(name: str) -> list[str]:
    if name in ("", ".", "..") or Path(name).name != name:
        return [f"overlay name {name} must be a plain folder name"]
    if KEY_SEPARATOR in name:
        return [f"overlay name {name} cannot hold {KEY_SEPARATOR}"]
    if name in base_roles():
        return [f"{name} is a base role; an overlay needs its own name"]
    if (install.PROFILES_DIR / name).is_dir():
        return [f"{name} is a built in profile and would shadow the overlay"]
    return []


def _manifest(folder: Path) -> tuple[dict | None, str | None]:
    path = folder / "profile.yml"
    if not path.is_file():
        return None, "profile.yml is missing"
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError:
        return None, "profile.yml is not valid YAML"
    if not isinstance(data, dict):
        return None, "profile.yml must be a mapping"
    return data, None


def problems(folder: Path) -> list[str]:
    data, error = _manifest(folder)
    if error:
        return [error]
    found = [] if data.get("kind") == "overlay" else ["kind must be overlay"]
    found += _role_problems(data.get("wears"))
    if "extends" in data:
        found.append("an overlay has no extends")
    if data.get("name", folder.name) != folder.name:
        found.append(f"name {data['name']} does not match the folder {folder.name}")
    return found + _name_problems(folder.name)


def _overlays_dir() -> Path | None:
    bundle = install._get_bundle_path()
    return bundle / "profiles" if bundle else None


def overlay_new(name: str, wears: str) -> int:
    profiles = _overlays_dir()
    if profiles is None:
        return _fail("no bundle linked; run agentihooks bundle new DIR first")
    roles = [role.strip() for role in wears.split(",") if role.strip()]
    found = _name_problems(name) + _role_problems(roles)
    if found:
        return _fail("; ".join(found))
    folder = profiles / name
    if folder.exists():
        return _fail(f"overlay {name} already exists at {folder}")
    manifest = {"name": name, "description": f"{name} overlay", "kind": "overlay", "wears": roles}
    folder.mkdir(parents=True)
    (folder / "profile.yml").write_text(yaml.safe_dump(manifest, sort_keys=False))
    (folder / "CLAUDE.md").write_text(f"# {name}\n\nWhat an agent wearing {name} knows and does.\n")
    _write_json(folder / ".claude" / ".mcp.json", {"mcpServers": {}})
    for sub in OVERLAY_FOLDERS:
        _keep(folder / ".claude" / sub)
    print(f"[OK] Overlay {name} at {folder}, worn by {', '.join(roles)}; commit it before a swarm wears it")
    return 0


def overlay_check(name: str) -> int:
    profiles = _overlays_dir()
    folder = profiles / name if profiles else None
    if folder is None or not folder.is_dir():
        return _fail(f"overlay {name} not found in the linked bundle")
    found = problems(folder)
    if found:
        return _fail(f"overlay {name}: " + "; ".join(found))
    print(f"[OK] Overlay {name} is valid")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agentihooks")
    sub = parser.add_subparsers(dest="command", required=True)
    bundle = sub.add_parser("bundle").add_subparsers(dest="action", required=True)
    bundle.add_parser("new", help="Lay out an empty bundle, git init it and link it").add_argument("dir")
    overlay = sub.add_parser("overlay").add_subparsers(dest="action", required=True)
    new = overlay.add_parser("new", help="Write an overlay profile skeleton into the linked bundle")
    new.add_argument("name")
    new.add_argument("--wears", required=True, help="Comma separated base roles the overlay sits on")
    overlay.add_parser("check", help="Validate an overlay in the linked bundle").add_argument("name")
    return parser


def main(argv: list[str]) -> int:
    args = _parser().parse_args(argv)
    if args.command == "bundle":
        return bundle_new(Path(args.dir).expanduser().resolve())
    if args.action == "new":
        return overlay_new(args.name, args.wears)
    return overlay_check(args.name)
