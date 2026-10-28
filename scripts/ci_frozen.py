import argparse
import json
import subprocess
from pathlib import Path

from scripts.gate_protected import FROZEN

CANDIDATES = "tests/registry/FROZEN_CANDIDATES"


def _git(root: Path, *args: str, data: bytes | None = None) -> bytes:
    return subprocess.run(["git", "-C", str(root), *args], input=data, check=True, capture_output=True).stdout


def _workflow(registry: dict, workflow: str) -> None:
    path = Path(workflow)
    if workflow not in registry or path.parent != Path(".github/workflows") or path.suffix not in {".yml", ".yaml"}:
        raise ValueError(f"Not a frozen workflow: {workflow}")


def announce(root: Path, workflow: str, candidate: Path) -> str:
    registry_path = root / FROZEN
    registry = json.loads(registry_path.read_bytes())
    _workflow(registry, workflow)
    content = candidate.read_bytes()
    blob = _git(root, "hash-object", "--stdin", data=content).decode().strip()
    stored = root / CANDIDATES / blob
    stored.parent.mkdir(parents=True, exist_ok=True)
    stored.write_bytes(content)
    if blob not in registry[workflow]:
        registry[workflow].append(blob)
    registry_path.write_text(json.dumps(registry, indent=2) + "\n")
    return blob


def land(root: Path, workflow: str, blob: str, base: str = "origin/dev") -> None:
    registry = json.loads(_git(root, "show", f"{base}:{FROZEN}"))
    _workflow(registry, workflow)
    if blob not in registry[workflow]:
        raise ValueError(f"Candidate not announced for {workflow}: {blob}")
    content = _git(root, "show", f"{base}:{CANDIDATES}/{blob}")
    if _git(root, "hash-object", "--stdin", data=content).decode().strip() != blob:
        raise ValueError(f"Stored candidate does not match announced blob: {blob}")
    (root / workflow).write_bytes(content)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Announce exact frozen workflow bytes, then land them after the announcement merges."
    )
    actions = parser.add_subparsers(dest="action", required=True)
    announce_parser = actions.add_parser(
        "announce", help="Commit the registry and stored candidate in an announcement PR."
    )
    announce_parser.add_argument("workflow")
    announce_parser.add_argument("candidate", type=Path)
    land_parser = actions.add_parser("land", help="Restore an accepted candidate from the integration branch.")
    land_parser.add_argument("workflow")
    land_parser.add_argument("blob")
    land_parser.add_argument("--base", default="origin/dev")
    args = parser.parse_args(argv)
    root = Path(_git(Path.cwd(), "rev-parse", "--show-toplevel").decode().strip())
    if args.action == "announce":
        print(announce(root, args.workflow, args.candidate))
    else:
        land(root, args.workflow, args.blob, args.base)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
