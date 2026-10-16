import argparse
import hashlib
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

REGISTRY = Path(".github/input-keys.json")
KEYER = ("scripts/ci_input_key.py", ".github/actions/input-key")


def _object(revision: str, path: str) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"{revision}:{path}"], capture_output=True, text=True
    )
    if result.returncode:
        raise SystemExit(f"declared input {path} is missing at {revision}")
    return result.stdout.strip()


def key(job: str, head: str = "HEAD", base: str = "", registry: Path = REGISTRY, day: str = "") -> str:
    entry = json.loads(registry.read_text())[job]
    if entry.get("base_paths") and not base:
        raise SystemExit(f"{job} declares base paths and needs --base")
    record = {
        "version": 1,
        "job": job,
        "entry": entry,
        "keyer": [_object(head, path) for path in KEYER],
        "paths": {path: _object(head, path) for path in entry["paths"]},
        "base_paths": {path: _object(base, path) for path in entry.get("base_paths", [])},
        "day": day or datetime.now(timezone.utc).date().isoformat(),
    }
    digest = hashlib.sha256(json.dumps(record, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return f"input-pass-{job}-{digest}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--job", required=True)
    parser.add_argument("--base", default="")
    args = parser.parse_args(argv)
    value = key(args.job, base=args.base)
    print(f"key={value}")
    if destination := os.environ.get("GITHUB_OUTPUT"):
        with Path(destination).open("a") as output:
            output.write(f"key={value}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
