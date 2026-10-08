import argparse
import hashlib
import json
import math
import statistics
from datetime import datetime
from pathlib import Path

from tests.duration_coverage import IncompleteDurations, collected_tests, validate_coverage

_ROOT = Path(__file__).parent.parent
# A cache stamped this close to the run's event may still have been uploading when an early shard looked for it.
SAVE_MARGIN_SECONDS = 300


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


def saved_before(folder: Path, run_time: str) -> bool:
    stamp = folder / "saved-at"
    if not run_time or not stamp.is_file():
        return False
    return int(stamp.read_text()) <= datetime.fromisoformat(run_time).timestamp() - SAVE_MARGIN_SECONDS


def _choose(version: str, restored: Path, run_time: str) -> None:
    committed = _ROOT / f".test_durations-{version}"
    if not committed.is_file():
        committed = _ROOT / ".test_durations"
    collected = collected_tests(_ROOT)
    if (restored / ".test_durations").is_file():
        if not saved_before(restored, run_time):
            print("Refusing the restored dev durations: saved after this run began or without a save time")
        else:
            durations = adopt(restored, version)
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


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("version", help="the Python version whose durations this shard splits on")
    parser.add_argument("restored", type=Path, help="the folder the dev durations cache restored into")
    parser.add_argument("--run-time", default="", help="the event time every shard of this run shares")
    parser.add_argument("--hash", type=Path, help="where to write the sha256 of the durations this shard splits on")
    args = parser.parse_args(argv)
    _choose(args.version, args.restored, args.run_time)
    if args.hash:
        args.hash.write_text(hashlib.sha256((_ROOT / ".test_durations").read_bytes()).hexdigest() + "\n")


if __name__ == "__main__":
    main()
