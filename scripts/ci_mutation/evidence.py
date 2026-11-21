import argparse
import shutil
from pathlib import Path


def stage(root: Path, source: Path, destination: Path) -> None:
    shutil.copytree(root / source, destination)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args(argv)
    stage(args.root, args.source, args.destination)


if __name__ == "__main__":
    main()
