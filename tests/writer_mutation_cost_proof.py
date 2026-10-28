import json
import sys
from pathlib import Path


def prepare(root: Path, baseline: Path) -> None:
    from mutmut.utils.format_utils import get_mutant_name

    from scripts.ci_mutation.selection import selected_mutant_names

    work = next(baseline.glob("*/changed-lines.json")).parent
    changes = json.loads((work / "changed-lines.json").read_text())
    plan = json.loads((work / "shard-plan.json").read_text())
    names = {
        get_mutant_name(Path(path), name)
        for path, change in changes.items()
        for name in selected_mutant_names(path, (root / path).read_text(), set(change["lines"]))
    }
    assert len(names) == 72 and names == set(plan["assignments"]), (len(names), len(plan["assignments"]))
    (root / ".mutation-selection.json").write_text(
        json.dumps({path: value["tests"] for path, value in changes.items()})
    )
    stats = root / ".mutation-stats"
    stats.mkdir(exist_ok=True)
    (stats / "plan.json").write_text(json.dumps(plan))
    print(f"Original workload: {len(names)} identical mutants across {len(plan['shards'])} shards", flush=True)


def verify(folder: Path, baseline: Path) -> None:
    plan_file = next(baseline.glob("*/shard-plan.json"))
    plan = json.loads(plan_file.read_text())
    reports = {path.parent: json.loads(path.read_text()) for path in folder.glob("*/report.json")}
    assert len(reports) == 10, len(reports)
    assert {report["shard"]["index"] for report in reports.values()} == set(range(10))
    checked = set()
    for work, report in reports.items():
        shard = report["shard"]
        index = shard["index"]
        assert shard["total"] == 10 and shard["budget_seconds"] == 1080
        assert shard["wall_seconds"] <= 1080 and shard["complete"] and not shard["budget_exhausted"], shard
        assert not report["not_mutated"], report["not_mutated"]
        expected = {
            name
            for name, assignment in plan["assignments"].items()
            if index in (assignment if isinstance(assignment, list) else [assignment])
        }
        actual = {
            row["name"]
            for results in work.glob("*/results.json")
            for rows in json.loads(results.read_text()).values()
            for row in rows
            if row["status"] != "not checked"
        }
        assert actual == expected, {
            "shard": index,
            "missing": sorted(expected - actual),
            "extra": sorted(actual - expected),
        }
        checked.update(actual)
    assert len(checked) == 72 and checked == set(plan["assignments"]), len(checked)
    print(json.dumps({"mutants": len(checked), "shards": [report["shard"] for report in reports.values()]}), flush=True)


if __name__ == "__main__":
    if sys.argv[1] == "prepare":
        prepare(Path.cwd(), Path(sys.argv[2]))
    else:
        verify(Path(sys.argv[2]), Path(sys.argv[3]))
