"""Which bundle manifestos a profile chain receives, and the role by manifesto matrix."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from hooks import config
from hooks.context import profile_chain

ENABLED = "CI_MANIFESTO_ENABLED"


def _env(root: Path) -> dict:
    from scripts.install import _NATIVE_SETTINGS_NAME, _load_native_layer, _native_layer_path

    path = _native_layer_path(root, "claude", _NATIVE_SETTINGS_NAME)
    return (_load_native_layer(path).get("env") or {}) if path else {}


def enabled(bundle: Path | None, dirs: list[tuple[str, Path]]) -> bool:
    value = "true"
    for root in ([bundle] if bundle is not None else []) + [path for _, path in dirs]:
        value = str(_env(root).get(ENABLED, value))
    return value.lower() in ("true", "1", "yes")


def _names(value) -> list[str]:
    return [str(name) for name in (value if isinstance(value, list) else [value] if value else [])]


def choice(dirs: list[tuple[str, Path]]) -> dict[str, bool]:
    chosen: dict[str, bool] = {}
    for _, path in dirs:
        picks = profile_chain.manifestos(path)
        for name in _names(picks.get("include")):
            chosen[config.manifesto_name(name)] = True
        for name in _names(picks.get("exclude")):
            chosen[config.manifesto_name(name)] = False
    return chosen


def paths(bundle: Path | None, dirs: list[tuple[str, Path]]) -> list[Path]:
    if not enabled(bundle, dirs):
        return []
    found = config._resolve_manifesto_paths(bundle, profile_chain.role(dirs), choice(dirs))
    return [Path(path) for path in found]


def roles() -> list[str]:
    return sorted(path.name for path in profile_chain.PACKAGE_ROLES.iterdir() if path.is_dir() and path.name[0] != "_")


def matrix(found: list[Path]) -> str:
    columns = roles()
    width = max(len("manifesto"), *(len(path.name) for path in found))
    lines = ["  ".join(["manifesto".ljust(width), *columns])]
    for path in found:
        allowed = config.manifesto_roles(path)
        cells = [("x" if allowed is None or role.casefold() in allowed else "-").ljust(len(role)) for role in columns]
        lines.append("  ".join([path.name.ljust(width), *cells]).rstrip())
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agentihooks manifestos")
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list", help="Print which package roles receive each bundle manifesto")
    listing.add_argument("--bundle", type=Path, help="Bundle to read (default: the linked bundle)")
    args = parser.parse_args(argv)
    bundle = args.bundle or profile_chain.bundle_path(profile_chain.read_state())
    found = [Path(path) for path in config._resolve_manifesto_paths(bundle)]
    if not found:
        folder = Path(bundle) / "manifestos" if bundle is not None else "the default manifesto folder"
        print(f"No manifestos in {folder}", file=sys.stderr)
        return 1
    sys.stdout.write(matrix(found))
    return 0


if __name__ == "__main__":
    sys.exit(main())
