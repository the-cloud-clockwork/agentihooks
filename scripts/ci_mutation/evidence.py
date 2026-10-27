import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from .runner import prepare_workspace

FILES = (
    "changed-lines.json",
    "shared-stats.json",
    "shard-plan.json",
    "results.json",
    "run.log",
    "report.log",
    "pyproject.toml",
)


def stage(root: Path, output: Path, bundle: Path, shard: tuple[int, int]) -> dict:
    bundle.mkdir(parents=True, exist_ok=True)
    failed = True
    if (output / "report.json").is_file():
        shutil.copyfile(output / "report.json", bundle / "report.json")
        failed = json.loads((output / "report.json").read_text())["failed"]
    groups = []
    for work in sorted(output.glob("*")) if failed else []:
        if not work.is_dir():
            continue
        kept = [name for name in FILES if (work / name).is_file()]
        if not kept:
            continue
        target = bundle / work.name
        target.mkdir()
        for name in kept:
            shutil.copyfile(work / name, target / name)
        if "changed-lines.json" not in kept:
            continue
        selection = json.loads((work / "changed-lines.json").read_text())
        parts = [str(part) for part in shard]
        command = ["python", "-m", "scripts.ci_mutation.selection", "changed-lines.json", *parts]
        if "shared-stats.json" in kept:
            command += ["reuse", "shared-stats.json"]
        groups.append(
            {
                "name": work.name,
                "commands": [
                    command,
                    ["python", "-m", "scripts.ci_mutation.report", "results.json", *parts, *selection],
                ],
            }
        )
    manifest = {
        "head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip(),
        "env": {
            key: os.environ[key]
            for key in ("CI", "PYTEST_DISABLE_PLUGIN_AUTOLOAD", "PYTHONUNBUFFERED")
            if key in os.environ
        },
        "groups": groups,
    }
    (bundle / "replay.json").write_text(json.dumps(manifest) + "\n")
    print(
        f"Mutation evidence: {sum(path.is_file() for path in bundle.rglob('*'))} files, "
        f"{sum(path.stat().st_size for path in bundle.rglob('*') if path.is_file())} bytes"
    )
    return manifest


def replay(root: Path, bundle: Path, work: Path, group: str) -> None:
    manifest = json.loads((bundle / "replay.json").read_text())
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    if head != manifest["head"]:
        raise ValueError(f"Replay requires revision {manifest['head']}; current revision is {head}")
    entry = next(entry for entry in manifest["groups"] if entry["name"] == group)
    evidence = bundle / group
    selection = json.loads((evidence / "changed-lines.json").read_text())
    tests = sorted({test for change in selection.values() for test in change["tests"]})
    work.mkdir(parents=True)
    prepare_workspace(root, work, list(selection), tests)
    for name in FILES:
        if (evidence / name).is_file():
            shutil.copyfile(evidence / name, work / name)
    for command in entry["commands"]:
        subprocess.run([sys.executable, *command[1:]], cwd=work, env=os.environ | manifest["env"], check=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    pack = commands.add_parser("stage")
    pack.add_argument("--output", type=Path, default=Path(".mutation-gate"))
    pack.add_argument("--bundle", type=Path, default=Path(".mutation-evidence"))
    pack.add_argument("--shard", type=int, required=True)
    pack.add_argument("--shards", type=int, required=True)
    restore = commands.add_parser(
        "replay", description="Check out replay.json's head and install its dependencies before replaying a group."
    )
    restore.add_argument("--bundle", type=Path, required=True)
    restore.add_argument("--work", type=Path, required=True)
    restore.add_argument("--group", required=True)
    args = parser.parse_args(argv)
    if args.command == "stage":
        stage(Path.cwd(), args.output, args.bundle, (args.shard, args.shards))
    else:
        replay(Path.cwd(), args.bundle, args.work, args.group)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
