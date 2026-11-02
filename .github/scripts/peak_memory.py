"""Run a command and record the peak resident memory of its largest process.

Usage: peak_memory.py LABEL -- COMMAND [ARG...]

Writes LABEL.json into PEAK_MEMORY_DIR (default RUNNER_TEMP/peak-memory), appends
one line to the job summary, and exits with the command's status. A SIGINT or
SIGTERM from a cancelled job is forwarded to the command, which is then recorded.
The figure is the largest single process among the command and its waited
descendants.
"""

import json
import os
import resource
import signal
import subprocess
import sys
from pathlib import Path


def record(label: str, command: list[str]) -> int:
    """Run command, record its peak resident memory under label and return its shell exit status."""
    child = subprocess.Popen(command)
    for forwarded in (signal.SIGINT, signal.SIGTERM):
        signal.signal(forwarded, lambda signum, _frame: child.send_signal(signum))
    status = child.wait()
    if status < 0:
        status = 128 - status
    peak_kib = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    peak_mib = round(peak_kib / 1024, 1)
    folder = Path(os.environ.get("PEAK_MEMORY_DIR") or Path(os.environ.get("RUNNER_TEMP", ".")) / "peak-memory")
    folder.mkdir(parents=True, exist_ok=True)
    entry = {"label": label, "peak_rss_kib": peak_kib, "peak_rss_mib": peak_mib, "exit": status}
    (folder / f"{label}.json").write_text(json.dumps(entry) + "\n")
    line = f"Peak resident memory of {label}: {peak_mib} MiB"
    print(line, file=sys.stderr)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    return status


def main(argv: list[str]) -> int:
    """Parse LABEL -- COMMAND and run it under record."""
    if len(argv) < 3 or argv[1] != "--":
        print("usage: peak_memory.py LABEL -- COMMAND [ARG...]", file=sys.stderr)
        return 2
    return record(argv[0], argv[2:])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
