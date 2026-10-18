import argparse
import hashlib
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from zipfile import ZipFile

from scripts.ci_mutation.clearances import load_clearances
from scripts.ci_mutation.measured import refine_plan
from scripts.ci_mutation.mutant_shards import unit_count
from scripts.ci_mutation.runner import grade_shard_rows, mutate_files, record_shard_measurement, split_plan_names
from scripts.ci_mutation.stats import SharedStats, stats_key, write_part

ARTIFACTS = {"second": 11756407264, "third": 11758451635}


def prepare(case: str, folder: Path) -> dict:
    folder.mkdir(parents=True, exist_ok=True)
    archive = folder / "retained.zip"
    with archive.open("wb") as stream:
        subprocess.run(
            ["gh", "api", f"repos/the-cloud-clockwork/agentihooks/actions/artifacts/{ARTIFACTS[case]}/zip"],
            stdout=stream,
            check=True,
        )
    source = folder / "source"
    with ZipFile(archive) as zipfile:
        prefix = next(name for name in zipfile.namelist() if name.endswith("/changed-lines.json")).rsplit("/", 1)[0]
        scope = json.loads(zipfile.read(f"{prefix}/changed-lines.json"))
        results = json.loads(zipfile.read(f"{prefix}/shared-stats.json"))
        expected = set()
        for name in zipfile.namelist():
            if name.startswith(f"{prefix}/mutants/") and name.endswith(".py.meta"):
                expected.update(json.loads(zipfile.read(name))["exit_code_by_key"])
            if not name.startswith(f"{prefix}/"):
                continue
            relative = Path(name.removeprefix(f"{prefix}/"))
            if name.endswith("/") or relative.parts[0] in {"mutants", "killers"}:
                continue
            target = source / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(zipfile.read(name))
    tests = sorted({test for change in scope.values() for test in change["tests"]})
    hashes = {name: hashlib.sha256((source / name).read_bytes()).hexdigest() for name in [*scope, *tests]}
    shutil.copytree(Path(__file__).parent, source / "scripts/ci_mutation", dirs_exist_ok=True)
    selected = {path: (set(change["lines"]), change["tests"]) for path, change in scope.items()}
    stats = SharedStats(folder / "stats", "retained")
    write_part(stats.group(0).folder / "part-0.json", stats_key(stats.head, selected), (0, 1), results)
    previous = Path.cwd()
    os.chdir(source)
    try:
        plan = refine_plan(source, selected, stats)
    finally:
        os.chdir(previous)
    if set(plan["assignments"]) != expected:
        raise ValueError("replay plan changed the retained mutant scope")
    (stats.folder / "plan.json").write_text(json.dumps(plan))
    (folder / "scope.json").write_text(json.dumps(scope))
    manifest = {"case": case, "artifact": ARTIFACTS[case], "source_test_sha256": hashes, "mutants": len(expected)}
    (folder / "manifest.json").write_text(json.dumps(manifest))
    print(json.dumps({**manifest, "shards": plan["shards"]}))
    if output := os.environ.get("GITHUB_OUTPUT"):
        with Path(output).open("a") as stream:
            stream.write(f"shards={json.dumps(list(range(plan['shards'])))}\n")
    return plan


def grade(folder: Path, index: int, total: int) -> dict:
    scope = json.loads((folder / "scope.json").read_text())
    selected = {path: (set(change["lines"]), change["tests"]) for path, change in scope.items()}
    output = folder / f"grade-{index}"
    output.mkdir()
    work = output / "work"
    work.mkdir()
    started = time.monotonic()
    stats = SharedStats(folder / "stats", "retained")
    rows, reason = mutate_files(folder / "source", work, selected, started + 1080, (index, total), stats.group(0))
    cleared = load_clearances(folder / "source")
    split = split_plan_names(stats)
    report = {"files": [], "not_mutated": [{"path": path, "reason": reason} for path in selected] if reason else []}
    if not reason:
        report["files"] = [
            grade_shard_rows(report, path, rows[path], lines, cleared, split) for path, (lines, _) in selected.items()
        ]
    report["failed"] = bool(reason) or any(file["failures"] for file in report["files"])
    record_shard_measurement(report, output, stats, started, 1080, (index, total))
    (output / "report.json").write_text(json.dumps(report))
    print(json.dumps(report))
    return report


def verify(folder: Path) -> dict:
    from scripts.ci_mutation.merge import merge_reports

    manifest = json.loads((folder / "manifest.json").read_text())
    plan = json.loads((folder / "stats/plan.json").read_text())
    reports = [json.loads(path.read_text()) for path in sorted((folder / "grades").glob("*/report.json"))]
    report = merge_reports(reports, load_clearances(folder / "source"))
    shards = report.get("shards", [])
    complete = (
        sorted(shard["index"] for shard in shards) == list(range(plan["shards"]))
        and all(shard["complete"] for shard in shards)
        and sum(shard["expected"] for shard in shards) == unit_count(plan)
        and sum(shard["graded"] for shard in shards) == unit_count(plan)
    )
    survivors = sum(row["status"] in {"survived", "no tests"} for file in report["files"] for row in file["failures"])
    proof = {
        "case": manifest["case"],
        "expected": unit_count(plan),
        "graded": sum(shard["graded"] for shard in shards),
        "exhausted_shards": sum(shard["budget_exhausted"] for shard in shards),
        "complete": complete,
        "survivor_grade_failed": bool(survivors) and report["failed"],
        "survivor_failures": survivors,
        "shards": shards,
        "files": report["files"],
    }
    (folder / "replay-proof.json").write_text(json.dumps(proof))
    print(json.dumps(proof))
    return proof


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["prepare", "grade", "verify"])
    parser.add_argument("--case", choices=list(ARTIFACTS), default="second")
    parser.add_argument("--folder", type=Path, default=Path(".mutation-replay"))
    parser.add_argument("--shard", type=int, default=0)
    parser.add_argument("--shards", type=int, default=1)
    args = parser.parse_args()
    folder = args.folder.resolve()
    if args.mode == "prepare":
        prepare(args.case, folder)
        return 0
    if args.mode == "verify":
        proof = verify(folder)
        return int(not proof["complete"] or proof["exhausted_shards"] != 0 or not proof["survivor_grade_failed"])
    report = grade(folder, args.shard, args.shards)
    return int(report["failed"] or not report["shard"]["complete"] or report["shard"]["budget_exhausted"])


if __name__ == "__main__":
    raise SystemExit(main())
