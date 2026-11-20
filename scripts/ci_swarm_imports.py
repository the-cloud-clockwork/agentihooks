import argparse
import ast
import re
import sys
from pathlib import Path

ALIASES = {
    "dotenv": "python-dotenv",
    "jwt": "pyjwt",
    "nats": "nats-py",
    "opentelemetry": "opentelemetry-api",
    "opentelemetry.sdk": "opentelemetry-sdk",
    "opentelemetry.exporter.otlp.proto.http": "opentelemetry-exporter-otlp-proto-http",
    "opentelemetry.exporter.otlp.proto.grpc": "opentelemetry-exporter-otlp-proto-grpc",
    "yaml": "pyyaml",
}


def distribution(module: str) -> str:
    parts = module.split(".")
    for length in range(len(parts), 0, -1):
        prefix = ".".join(parts[:length])
        if prefix in ALIASES:
            return ALIASES[prefix]
    return parts[0].lower().replace("_", "-")


def local_module(root: Path, path: Path, module: str) -> bool:
    return any(
        (folder / f"{module}.py").is_file() or (folder / module / "__init__.py").is_file()
        for folder in (root, path.parent)
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
    required: dict[str, set[str]] = {}
    for package in ("hooks", "scripts"):
        folder = root / package
        paths = sorted(folder.rglob("*.py"))
        if not paths:
            raise ValueError(f"No Python sources under {folder}")
        for path in paths:
            for qualified, line in module_imports(path):
                module = qualified.split(".")[0]
                if module not in sys.stdlib_module_names and not local_module(root, path, module):
                    location = f"{path.relative_to(root)}:{line}"
                    if location not in imported.setdefault(module, []):
                        imported[module].append(location)
                    required.setdefault(module, set()).add(distribution(qualified))
    lock = root / "docker" / "swarm" / "requirements.lock"
    pinned = {
        match.group(1).lower().replace("_", "-")
        for line in lock.read_text().splitlines()
        if (match := re.match(r"^([A-Za-z0-9_.-]+)==([^\s;*]+)(?:\s|$)", line))
    }
    return imported, {module for module, distributions in required.items() if not distributions <= pinned}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    imported, missing = check(args.root)
    for module, locations in sorted(imported.items()):
        status = "UNPINNED" if module in missing else "PINNED"
        print(f"{status} {module}: {', '.join(locations)}")
    print(f"{len(imported)} third party imports; {len(missing)} unpinned")
    return int(bool(missing))


if __name__ == "__main__":
    raise SystemExit(main())
