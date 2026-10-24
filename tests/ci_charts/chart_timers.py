import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
TIMED_KINDS = {"CronJob"}


def charts(root: Path) -> list[Path]:
    found = sorted(p.parent for p in (root / "deploy").rglob("Chart.yaml"))
    return [c for c in found if not any(other in c.parents for other in found)]


def values_files(chart: Path) -> list[Path | None]:
    skipped = {chart / name for name in ("Chart.yaml", "Chart.lock", "values.yaml")}
    found = [p for pattern in ("*.yaml", "*.yml") for p in chart.rglob(pattern)]
    return [None] + sorted(
        p for p in found if p not in skipped and not {"templates", "charts"} & set(p.relative_to(chart).parts)
    )


def render(chart: Path, values: Path | None) -> subprocess.CompletedProcess:
    command = ["helm", "template", "timer-check", str(chart)] + (["-f", str(values)] if values else [])
    return subprocess.run(command, capture_output=True, text=True)


def timed_objects(documents: list) -> list[str]:
    found = []
    for item in documents:
        if not isinstance(item, dict):
            continue
        if item.get("kind") in TIMED_KINDS:
            found.append(f"{item['kind']}/{item.get('metadata', {}).get('name')}")
        found += timed_objects(item.get("items") or [])
    return found


def main() -> int:
    found = charts(ROOT)
    if not found:
        print("No chart found under deploy; the check graded nothing.")
        return 1
    offenders, failures, renders = [], [], 0
    for chart in found:
        for values in values_files(chart):
            renders += 1
            source = f"{chart.relative_to(ROOT)} with {values.relative_to(ROOT) if values else 'default values'}"
            result = render(chart, values)
            if result.returncode:
                failures.append(f"{source} failed to render: {result.stderr.strip()}")
                continue
            documents = list(yaml.safe_load_all(result.stdout))
            offenders += [f"{source}: {obj}" for obj in timed_objects(documents)]
    for line in failures + offenders:
        print(line)
    print(
        f"{len(offenders)} rendered CronJob objects and {len(failures)} failed renders across {renders} renders of {len(found)} charts."
    )
    if offenders:
        print(
            "A CronJob schedules work outside the agentihooks scheduler. Register the job with scripts.scheduler instead."
        )
    return 1 if offenders or failures else 0


if __name__ == "__main__":
    sys.exit(main())
