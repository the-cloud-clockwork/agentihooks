import argparse
import json
import math
import statistics
from pathlib import Path

from tests.duration_coverage import IncompleteDurations, collected_tests, validate_coverage

_ROOT = Path(__file__).parent.parent


def _durations(path: Path) -> dict[str, float]:
    data = json.loads(path.read_text())
    if (
        not isinstance(data, dict)
        or not data
        or not all(isinstance(v, (int, float)) and math.isfinite(v) and v >= 0 for v in data.values())
    ):
        raise ValueError(f"{path.name} holds no durations")
    return data


def adopt(folder: Path, version: str) -> dict[str, float]:
    measured = folder / f".test_durations-{version}"
    return {**_durations(folder / ".test_durations"), **(_durations(measured) if measured.exists() else {})}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("version", help="the Python version whose durations this shard splits on")
    parser.add_argument("restored", type=Path, help="the folder the dev durations cache restored into")
    args = parser.parse_args(argv)
    committed = _ROOT / f".test_durations-{args.version}"
    if not committed.is_file():
        committed = _ROOT / ".test_durations"
    collected = collected_tests(_ROOT)
    if (args.restored / ".test_durations").is_file():
        durations = adopt(args.restored, args.version)
        try:
            validate_coverage(durations, collected)
        except IncompleteDurations as error:
            print(f"Refusing the restored dev durations: {error}")
        else:
            (_ROOT / ".test_durations").write_text(json.dumps(durations, indent=4, sort_keys=True) + "\n")
            print(f"Using {len(durations)} restored dev durations")
            return
    try:
        validate_coverage(_durations(committed), collected)
    except IncompleteDurations:
        committed = _ROOT / ".test_durations"
        try:
            validate_coverage(_durations(committed), collected)
        except IncompleteDurations as error:
            durations = _durations(committed)
            even = dict.fromkeys(collected, statistics.median(durations.values()))
            (_ROOT / ".test_durations").write_text(json.dumps({**even, **durations}, indent=4, sort_keys=True) + "\n")
            print(f"::warning::Splitting tests without a committed duration evenly: {error}")
            return
    if committed != _ROOT / ".test_durations":
        (_ROOT / ".test_durations").write_bytes(committed.read_bytes())
    print("Using committed durations")


if __name__ == "__main__":
    main()
