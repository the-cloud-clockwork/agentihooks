import argparse
import json
from pathlib import Path

from scripts.ci_mutation.stats_dependencies import SHARED


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("folder", type=Path)
    folder = parser.parse_args(argv).folder
    files = list(folder.iterdir())
    records = [path for path in files if path.suffix == ".json"]
    referenced = set()
    for path in records:
        record = json.loads(path.read_text())
        referenced.add(record["inputs"])
        referenced.update(record.get("dependencies", {}).values())
    shared = {path.stem: path.stat().st_size for path in files if path.suffix == SHARED}
    orphans = {key: size for key, size in shared.items() if key not in referenced}
    print(
        json.dumps(
            {
                "records": len(records),
                "shared_files": len(shared),
                "shared_bytes": sum(shared.values()),
                "orphan_files": len(orphans),
                "orphan_bytes": sum(orphans.values()),
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
