import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

from scripts.ci_mutation.plan import estimate
from scripts.ci_mutation.runner import mutate_files
from scripts.ci_mutation.scope import discover_changes, select_tests
from scripts.ci_mutation.selection import file_seconds
from tests.dep_manifest import build


def prepare(root: Path, folder: Path) -> None:
    changes = discover_changes(root, ["HEAD^"], "HEAD")
    tests = sorted({test for path in changes for test in select_tests(root, Path(path))})
    folder.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            *tests,
            "-o",
            "addopts=",
            "--cov=hooks",
            "--cov=scripts",
            "--cov-context=test",
            "--cov-report=",
            "-q",
        ],
        check=True,
    )
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    manifest = build(root / ".coverage", [], root, commit, sys.version)
    selections = {}
    for label in ("before", "after"):
        chosen = {}
        record = manifest if label == "after" else {key: value for key, value in manifest.items() if key != "lines"}
        _, _, selected = estimate(root, changes, chosen, record)
        selections[label] = {
            "tests": selected,
            "selection": chosen,
            "seconds": sum(file_seconds(root, selected).values()),
        }
    report = {"commit": commit, "changes": {path: sorted(lines) for path, lines in changes.items()}, **selections}
    (folder / "selection.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report), flush=True)
    assert selections["after"]["seconds"] < selections["before"]["seconds"]


def grade(root: Path, folder: Path, label: str) -> None:
    report = json.loads((folder / "selection.json").read_text())
    path = "scripts/swarm/health/verdicts.py"
    selected = {path: (set(report["changes"][path]), report[label]["selection"][path])}
    work = folder / label
    work.mkdir()
    rows, reason = mutate_files(root, work, selected, time.monotonic() + 1500, (0, 1), None)
    assert not reason, reason
    verdicts = {row["name"]: row["status"] for row in rows[path]}
    assert verdicts and "not checked" not in verdicts.values()
    (folder / f"{label}-verdicts.json").write_text(json.dumps(verdicts, indent=2))
    print(json.dumps({label: verdicts}), flush=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["prepare", "before", "after", "verify"])
    parser.add_argument("--folder", type=Path, required=True)
    args = parser.parse_args()
    root, folder = Path.cwd(), args.folder.resolve()
    if args.mode == "prepare":
        prepare(root, folder)
    elif args.mode == "verify":
        before = json.loads((folder / "before-verdicts.json").read_text())
        after = json.loads((folder / "after-verdicts.json").read_text())
        assert before == after, {
            name: (status, after.get(name)) for name, status in before.items() if after.get(name) != status
        }
        print(f"Identical verdicts for {len(before)} sample pull request mutants", flush=True)
    else:
        grade(root, folder, args.mode)


if __name__ == "__main__":
    main()
