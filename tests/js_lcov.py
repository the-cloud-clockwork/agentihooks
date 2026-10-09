"""Turn the node runs the tests captured into an lcov report on the page sources.

Tests run page functions through `node -e` on text cut verbatim from the sources, so a
captured script's lines are mapped back to the source lines they were copied from.
"""

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

MIN_MATCH_CHARS = 24


def normalized(line: str) -> str:
    return line.strip().removeprefix("export ").strip()


def executable(line: str) -> bool:
    text = line.strip()
    return bool(text.strip("{}()[];,")) and not text.startswith(("import ", "//", "/*", "*"))


def line_hits(script: str, functions: list[dict]) -> list[int | None]:
    units = []
    for char in script:
        units.append(char)
        if ord(char) > 0xFFFF:
            units.append("")
    counts = [0] * len(units)
    ranges = [r for fn in functions for r in fn["ranges"]]
    for r in sorted(ranges, key=lambda r: r["startOffset"] - r["endOffset"]):
        start, end = r["startOffset"], min(r["endOffset"], len(units))
        counts[start:end] = [r["count"]] * (end - start)
    hits, line = [], None
    for unit, count in zip(units + ["\n"], counts + [0]):
        if unit == "\n":
            hits.append(line)
            line = None
        elif unit.strip():
            line = max(line or 0, count)
    return hits


class Sources:
    def __init__(self, root: Path):
        self.files = {
            path.relative_to(root).as_posix(): path.read_bytes().decode("utf-8").split("\n")
            for path in sorted(root.glob("scripts/**/*.js"))
        }
        self.lines = {name: [normalized(line) for line in lines] for name, lines in self.files.items()}
        self.index = defaultdict(list)
        for name, lines in self.lines.items():
            for number, text in enumerate(lines):
                if text:
                    self.index[text].append((name, number))
        self.hits = {name: defaultdict(int) for name in self.files}

    def run_at(self, script: list[str], at: int, name: str, start: int) -> int:
        source = self.lines[name]
        length = 0
        while (
            at + length < len(script) and start + length < len(source) and script[at + length] == source[start + length]
        ):
            length += 1
        return length

    def attribute(self, script: str, hits: list[int | None]) -> None:
        lines = [normalized(line) for line in script.split("\n")]
        at = 0
        while at < len(lines):
            runs = [(self.run_at(lines, at, name, start), name, start) for name, start in self.index.get(lines[at], ())]
            length = max((run[0] for run in runs), default=0)
            if length and sum(len(text) for text in lines[at : at + length]) >= MIN_MATCH_CHARS:
                for _, name, start in (run for run in runs if run[0] == length):
                    for offset in range(length):
                        if hits[at + offset] is not None:
                            self.hits[name][start + offset] += hits[at + offset]
                at += length
            else:
                at += 1

    def lcov(self) -> str:
        out = []
        for name, lines in self.files.items():
            out += ["TN:", f"SF:{name}"]
            found = hit = 0
            for number, line in enumerate(lines):
                if executable(line):
                    count = self.hits[name][number]
                    out.append(f"DA:{number + 1},{count}")
                    found += 1
                    hit += count > 0
            out += [f"LF:{found}", f"LH:{hit}", "end_of_record"]
        return "\n".join(out) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--captures", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    captures = sorted(args.captures.rglob("capture-*.json"))
    totals, kinds = {}, set()
    for capture in captures:
        for entry in json.loads(capture.read_text(encoding="utf-8"))["result"]:
            kinds.add(capture.name.split("-")[1])
            stored = capture.parent / "sources" / f"{entry['source']}.js"
            script = stored.read_bytes().decode("utf-8")
            hits = line_hits(script, entry["functions"])
            summed = totals.setdefault(stored.name, (script, [None] * len(hits)))[1]
            for number, count in enumerate(hits):
                if count is not None:
                    summed[number] = (summed[number] or 0) + count
    missing = sorted({"browser", "node"} - kinds)
    if missing:
        print(f"::error::No {' or '.join(missing)} coverage recorded under {args.captures}")
        return 1
    sources = Sources(args.root)
    for script, hits in totals.values():
        sources.attribute(script, hits)
    args.out.write_text(sources.lcov(), encoding="utf-8")
    print(f"Mapped {len(captures)} captured runs of {len(totals)} scripts onto {len(sources.files)} page sources")
    return 0


if __name__ == "__main__":
    sys.exit(main())
