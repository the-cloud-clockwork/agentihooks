import argparse
import shutil
from pathlib import Path


def stage(root: Path, source: Path, destination: Path) -> None:
    source = root / source
    if not source.is_dir():
        return
    workspaces = sorted(
        marker.parent for marker in source.rglob("changed-lines.json") if (marker.parent / "pyproject.toml").is_file()
    )

    def ignore(directory: str, names: list[str]) -> set[str]:
        path = Path(directory)
        ignored = {
            name
            for name in names
            if name in {".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"} or name.endswith(".pyc")
        }
        workspace = next((work for work in workspaces if path.is_relative_to(work)), None)
        if workspace is None:
            return ignored
        relative = path.relative_to(workspace)
        nested = relative.parts[:1] == ("mutants",)
        if nested:
            relative = Path(*relative.parts[1:])
        if nested or workspace != workspaces[0]:
            ignored.update(
                name
                for name in names
                if (root / relative / name).is_file() and (relative != Path(".") or name != "pyproject.toml")
            )
        return ignored

    shutil.copytree(source, destination, ignore=ignore)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("root")
    parser.add_argument("source")
    parser.add_argument("destination")
    args = parser.parse_args(argv)
    stage(Path(args.root), Path(args.source), Path(args.destination))


if __name__ == "__main__":
    main()
