import json
import sys
from pathlib import Path

DURATIONS = ("durations-3.*/durations.json", "durations.json")
FLAKES = ("flakes-3.*/flakes.jsonl", "flakes.jsonl")
TEST = "test"
OUTCOME = "outcome"
FLAKY = "flaky"
FAILED = "failed"


def _found(reports: Path, patterns: tuple[str, ...]) -> list[Path]:
    """Return each report a per artifact folder holds, and the one a lone matched artifact left at the folder root."""
    return [path for pattern in patterns for path in reports.glob(pattern)]


def counts(reports: Path) -> dict[str, int]:
    ran: set[tuple[str, str]] = set()
    outcomes: dict[tuple[str, str], str] = {}
    for path in _found(reports, DURATIONS):
        shard = path.parent.name.removeprefix("durations-")
        ran.update((shard, test) for test in json.loads(path.read_text()))
    for path in _found(reports, FLAKES):
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
