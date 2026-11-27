"""Write coverage.xml from the combined data with one coverage xml process per part.

The measured files are split by directory into parts of about equal source size, so
every package lands whole in one part. The parts run beside each other and are joined
into one report sorted by package, with the root totals summed. A TOTAL line names the
lines and branches covered.
"""

import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

from coverage import Coverage
from coverage.xmlreport import rate

TOTALS = ("lines-valid", "lines-covered", "branches-valid", "branches-covered")


def size(files: list[str]) -> int:
    """Return the bytes of the files that exist."""
    return sum(path.stat().st_size for path in map(Path, files) if path.is_file())


def split(files: list[str], count: int) -> list[list[str]]:
    """Split the files by directory into at most count parts, each directory onto the lightest part."""
    folders: dict[str, list[str]] = defaultdict(list)
    for name in files:
        folders[str(Path(name).parent)].append(name)
    weights = {folder: size(names) for folder, names in folders.items()}
    parts: list[list[str]] = [[] for _ in range(max(count, 1))]
    loads = [0] * len(parts)
    for folder in sorted(folders, key=weights.__getitem__, reverse=True):
        lightest = loads.index(min(loads))
        parts[lightest].extend(folders[folder])
        loads[lightest] += weights[folder]
    return [part for part in parts if part] or [[]]


def join(reports: list[Path], out: Path) -> None:
    """Write the reports as one, its packages sorted by name and its root totals summed."""
    roots = [
        # nosemgrep: python.lang.security.use-defused-xml-parse.use-defused-xml-parse
        ET.parse(report, ET.XMLParser(target=ET.TreeBuilder(insert_comments=True))).getroot()
        for report in reports
    ]
    joined = roots[0]
    packages = [package for root in roots for package in root.iterfind("packages/package")]
    joined.find("packages")[:] = sorted(packages, key=lambda package: package.get("name", ""))
    totals = {key: sum(int(root.get(key, "0")) for root in roots) for key in TOTALS}
    joined.attrib.update({key: str(value) for key, value in totals.items()})
    joined.set("line-rate", rate(totals["lines-covered"], totals["lines-valid"]))
    joined.set("branch-rate", rate(totals["branches-covered"], totals["branches-valid"]))
    ET.ElementTree(joined).write(out, encoding="utf-8", xml_declaration=True)


def main(config: str, workers: int, out: Path) -> int:
    """Write out from parallel parts and print its TOTAL line; return the first failed part's exit code."""
    coverage = Coverage(config_file=config)
    coverage.load()
    parts = split(sorted(coverage.get_data().measured_files()), workers)
    with tempfile.TemporaryDirectory() as folder:
        reports = [Path(folder) / f"{index}.xml" for index in range(len(parts))]
        running = [
            subprocess.Popen(
                [sys.executable, "-m", "coverage", "xml", "-q", f"--rcfile={config}", "-o", str(report), *part]
            )
            for report, part in zip(reports, parts)
        ]
        failed = [code for code in (process.wait() for process in running) if code]
        if failed:
            return failed[0]
        if len(reports) == 1:
            shutil.move(reports[0], out)
        else:
            join(reports, out)
    _, root = next(ET.iterparse(out, events=("start",)))
    lines = f"{root.get('lines-covered')} of {root.get('lines-valid')}"
    branches = f"{root.get('branches-covered')} of {root.get('branches-valid')}"
    print(f"TOTAL {lines} lines and {branches} branches covered, from {len(parts)} parts")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], int(sys.argv[2]), Path(sys.argv[3])))
