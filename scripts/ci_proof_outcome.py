import re
import sys
from pathlib import Path

COMPLETED = ("passed", "failed", "xfailed", "xpassed")
ANSI = re.compile(r"\x1b\[[0-9;]*m")
INTERNAL = re.compile(r"^(?:\[gw\d+\] )?INTERNALERROR>", re.M)


def summary(line: str) -> str | None:
    line = line.strip()
    head, found, tail = line.strip("= ").rpartition(" in ")
    return head if found and line[:1] == line[-1:] == "=" and tail[:1].isdigit() else None


def verdict(log: str) -> str | None:
    log = ANSI.sub("", log)
    if INTERNAL.search(log):
        return "pytest raised an internal error."
    summaries = [found for line in log.splitlines() if (found := summary(line))]
    parts = [part.partition(" ") for part in summaries[-1].split(", ")] if summaries else []
    counts = {name: int(n) for n, _, name in parts if n.isdigit()}
    if not sum(counts.get(name, 0) for name in COMPLETED):
        return "No selected test completed."
    return None


def main(argv: list[str]) -> int:
    reason = verdict(Path(argv[0]).read_text(errors="replace"))
    if reason:
        print(f"::error title=Focused proof::{reason}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
