import json
import sys
from pathlib import Path


def prepare(root: Path, baseline: Path) -> None:
    from mutmut import get_mutant_name

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


def verify(folder: Path) -> None:
    reports = [json.loads(path.read_text()) for path in folder.glob("*/report.json")]
    assert len(reports) == 10, len(reports)
    for report in reports:
        shard = report["shard"]
        assert shard["budget_seconds"] == 1080
        assert shard["complete"] and not shard["budget_exhausted"], shard
        assert not report["not_mutated"], report["not_mutated"]
    print(json.dumps({"shards": [report["shard"] for report in reports]}), flush=True)


if __name__ == "__main__":
    if sys.argv[1] == "prepare":
        prepare(Path.cwd(), Path(sys.argv[2]))
    else:
        verify(Path(sys.argv[2]))
