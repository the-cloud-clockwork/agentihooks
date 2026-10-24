import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
TIMED_KINDS = {"CronJob"}


def charts(root: Path) -> list[Path]:
    return sorted(p.parent for p in (root / "deploy").rglob("Chart.yaml") if "charts" not in p.relative_to(root).parts)


def values_files(chart: Path) -> list[Path | None]:
    return [None] + sorted(p for folder in ("ci", "examples") for p in (chart / folder).glob("*.yaml"))


def render(chart: Path, values: Path | None) -> str:
    command = ["helm", "template", "timer-check", str(chart)] + (["-f", str(values)] if values else [])
    return subprocess.run(command, check=True, capture_output=True, text=True).stdout


def timed_objects(manifest: str) -> list[str]:
    found = []
    for document in yaml.safe_load_all(manifest):
        if not isinstance(document, dict):
            continue
        for item in document.get("items") or [document]:
            if isinstance(item, dict) and item.get("kind") in TIMED_KINDS:
                found.append(f"{item['kind']}/{item.get('metadata', {}).get('name')}")
    return found


def main() -> int:
    found = charts(ROOT)
    if not found:
        print("No chart found under deploy; the check graded nothing.")
        return 1
    offenders, renders = [], 0
    for chart in found:
        for values in values_files(chart):
            renders += 1
            source = values.relative_to(ROOT) if values else "default values"
            offenders += [f"{chart.relative_to(ROOT)} with {source}: {obj}" for obj in timed_objects(render(chart, values))]
    for offender in offenders:
        print(offender)
    print(f"{len(offenders)} rendered CronJob objects across {renders} renders of {len(found)} charts.")
    if offenders:
        print("A CronJob schedules work outside the agentihooks scheduler. Register the job with scripts.scheduler instead.")
    return 1 if offenders else 0


if __name__ == "__main__":
    sys.exit(main())
