import argparse
import ast
import re
import sys
from pathlib import Path

ALIASES = {
    "dotenv": "python-dotenv",
    "jwt": "pyjwt",
    "markdown_it": "markdown-it-py",
    "nats": "nats-py",
    "opentelemetry": "opentelemetry-api",
    "opentelemetry.exporter.otlp.proto.common": "opentelemetry-exporter-otlp-proto-common",
    "opentelemetry.exporter.otlp.proto.grpc": "opentelemetry-exporter-otlp-proto-grpc",
    "opentelemetry.exporter.otlp.proto.http": "opentelemetry-exporter-otlp-proto-http",
    "opentelemetry.proto": "opentelemetry-proto",
    "opentelemetry.sdk": "opentelemetry-sdk",
    "yaml": "pyyaml",
}


def distribution(module: str) -> str:
    return ALIASES.get(module, module).lower().replace("_", "-")


def import_name(module: str) -> str:
    parts = module.split(".")
    prefixes = (".".join(parts[:end]) for end in range(len(parts), 1, -1))
    return next((prefix for prefix in prefixes if prefix in ALIASES), parts[0])


def local_module(root: Path, path: Path, module: str) -> bool:
    """Bare imports resolve at the root or in a nested folder on the path, never beside the installed top level packages."""
    return any(
        (folder / f"{module}.py").is_file() or (folder / module / "__init__.py").is_file()
        for folder in (path.parent, *path.parent.parents)
        if folder.is_relative_to(root) and folder.parent != root
    )


def module_imports(path: Path) -> list[tuple[str, int]]:
    imported: list[tuple[str, int]] = []
    for node in ast.parse(path.read_text(), filename=str(path)).body:
        if isinstance(node, ast.Import):
            imported.extend((alias.name, node.lineno) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imported.extend((f"{node.module}.{alias.name}", node.lineno) for alias in node.names)
    return imported


def check(root: Path) -> tuple[dict[str, list[str]], set[str]]:
    imported: dict[str, list[str]] = {}
    for package in ("hooks", "scripts"):
        folder = root / package
        paths = sorted(folder.rglob("*.py"))
        if not paths:
            raise ValueError(f"No Python sources under {folder}")
        for path in paths:
            for module, line in module_imports(path):
                top = module.split(".")[0]
                if top not in sys.stdlib_module_names and not local_module(root, path, top):
                    location = f"{path.relative_to(root)}:{line}"
                    locations = imported.setdefault(import_name(module), [])
                    if location not in locations:
                        locations.append(location)
    lock = root / "docker" / "swarm" / "requirements.lock"
    pinned = {
        match.group(1).lower().replace("_", "-")
        for line in lock.read_text().splitlines()
        if (match := re.match(r"^([A-Za-z0-9_.-]+)==([^\s;*]+)(?:\s|$)", line))
    }
    return imported, {module for module in imported if distribution(module) not in pinned}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    imported, missing = check(args.root)
    for module, locations in sorted(imported.items()):
        status = "UNPINNED" if module in missing else "PINNED"
        print(f"{status} {module} ({distribution(module)}): {', '.join(locations)}")
    print(f"{len(imported)} third party imports; {len(missing)} unpinned")
    return int(bool(missing))


if __name__ == "__main__":
    raise SystemExit(main())
