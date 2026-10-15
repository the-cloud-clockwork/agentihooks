import json
import subprocess
import sys
import tempfile
from pathlib import Path


def timed_out_only(report: Path) -> list[str]:
    try:
        document = json.loads(report.read_text())
    except (OSError, ValueError):
        return []
    errors = document.get("errors", [])
    if document.get("results") or any(error.get("type") != "Timeout" for error in errors):
        return []
    return sorted({f"{error.get('rule_id')} on {error.get('path')}" for error in errors})


def scan(command: list[str], report: Path) -> int:
    code = subprocess.run([*command, f"--json-output={report}"]).returncode
    timeouts = timed_out_only(report) if code else []
    if not timeouts:
        return code
    print(f"Semgrep timed out with no other error, scanning once more: {', '.join(timeouts)}", file=sys.stderr)
    return subprocess.run([*command, f"--json-output={report}"]).returncode


def main(argv: list[str]) -> int:
    with tempfile.TemporaryDirectory() as folder:
        return scan(argv, Path(folder) / "semgrep.json")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
