import argparse
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
TIMED_KINDS = {"CronJob"}
REQUIRED_VALUES = Path("ci") / "required-values.yaml"


def charts(root: Path) -> list[Path]:
    found = sorted(p.parent for p in (root / "deploy").rglob("Chart.yaml"))
    return [c for c in found if not any(other in c.parents for other in found)]


def tree(argv: list[str] | None = None) -> Path:
    """Return the repository whose charts are graded: --root, else this checkout."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=ROOT)
    return parser.parse_args(argv).root


def values_files(chart: Path) -> list[Path | None]:
    skipped = {chart / name for name in ("Chart.yaml", "Chart.lock", "values.yaml", REQUIRED_VALUES)}
    found = [p for pattern in ("*.yaml", "*.yml") for p in chart.rglob(pattern)]
    return [None] + sorted(
        p for p in found if p not in skipped and not {"templates", "charts"} & set(p.relative_to(chart).parts)
    )


def render(chart: Path, values: Path | None) -> subprocess.CompletedProcess:
    """Render chart over its ci/required-values.yaml when it ships one, then over values when given."""
    required = chart / REQUIRED_VALUES
    command = ["helm", "template", "timer-check", str(chart)]
    command += ["-f", str(required)] if required.is_file() else []
    command += ["-f", str(values)] if values else []
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


def main(argv: list[str] | None = None) -> int:
    root = tree(argv)
    found = charts(root)
    if not found:
        print("No chart found under deploy; the check graded nothing.")
        return 1
    offenders, failures, renders = [], [], 0
    for chart in found:
        for values in values_files(chart):
            renders += 1
            source = f"{chart.relative_to(root)} with {values.relative_to(root) if values else 'default values'}"
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
