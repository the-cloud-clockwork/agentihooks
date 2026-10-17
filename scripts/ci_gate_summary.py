import json
import sys
from pathlib import Path

DURATIONS = "durations-3.*/durations.json"
FLAKES = "flakes-3.*/flakes.jsonl"
TEST = "test"
OUTCOME = "outcome"
FLAKY = "flaky"
FAILED = "failed"


def counts(reports: Path) -> dict[str, int]:
    ran = set()
    outcomes = {}
    for path in reports.glob(DURATIONS):
        shard = path.parent.name.removeprefix("durations-")
        ran.update((shard, test) for test in json.loads(path.read_text()))
    for path in reports.glob(FLAKES):
        shard = path.parent.name.removeprefix("flakes-")
        for line in path.read_text().splitlines():
            report = json.loads(line)
            key = (shard, report[TEST])
            ran.add(key)
            outcomes[key] = report[OUTCOME]
    return {"ran": len(ran), FLAKY: list(outcomes.values()).count(FLAKY), FAILED: list(outcomes.values()).count(FAILED)}


def main() -> None:
    result = counts(Path(sys.argv[1]))
    print(f"Tests: ran {result['ran']}, flaky {result[FLAKY]}, failed {result[FAILED]}")


if __name__ == "__main__":
    main()
