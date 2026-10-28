import re
import sys
from pathlib import Path

COMPLETED = ("passed", "failed", "xfailed", "xpassed")
ANSI = re.compile(r"\x1b\[[0-9;]*m")
SUMMARY = re.compile(r"^=+ (.+) in [\d.]+s(?: \([^)]*\))? =+$")
COUNT = re.compile(r"(\d+) (\w+)")


def verdict(log: str) -> str | None:
    log = ANSI.sub("", log)
    if "INTERNALERROR" in log:
        return "pytest raised an internal error."
    summaries = [m.group(1) for line in log.splitlines() if (m := SUMMARY.match(line.strip()))]
    counts = {name: int(n) for n, name in COUNT.findall(summaries[-1])} if summaries else {}
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
