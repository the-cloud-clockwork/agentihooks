"""Fails when a chart version bump alone changes a rendered pod template, which rolls every pod on each release."""

import shutil
import sys
import tempfile
from pathlib import Path

import yaml
from chart_timers import ROOT, charts, render, values_files

WORKLOAD_KINDS = {"Deployment", "StatefulSet", "DaemonSet", "ReplicaSet", "Job"}
PROBE_VERSION = "999.999.999"


def bumped_copy(chart: Path, into: Path) -> Path:
    """Copy the chart and give the copy a different chart version."""
    copy = into / chart.name
    shutil.copytree(chart, copy)
    meta = yaml.safe_load((copy / "Chart.yaml").read_text())
    meta["version"] = PROBE_VERSION
    (copy / "Chart.yaml").write_text(yaml.safe_dump(meta, sort_keys=False))
    return copy


def pod_templates(text: str) -> dict[str, object]:
    """Map each rendered workload to its pod template."""
    found = {}
    for item in yaml.safe_load_all(text):
        if isinstance(item, dict) and item.get("kind") in WORKLOAD_KINDS:
            found[f"{item['kind']}/{item['metadata']['name']}"] = item["spec"]["template"]
    return found


def template_drift(before: dict[str, object], after: dict[str, object]) -> list[str]:
    """Name every workload whose pod template differs between two renders."""
    return sorted(name for name in before.keys() | after.keys() if before.get(name) != after.get(name))


def main() -> int:
    """Render every chart and values file at two chart versions and compare the pod templates."""
    found = charts(ROOT)
    if not found:
        print("No chart found under deploy; the check graded nothing.")
        return 1
    offenders, failures, compared = [], [], 0
    with tempfile.TemporaryDirectory() as scratch:
        for chart in found:
            copy = bumped_copy(chart, Path(scratch))
            for values in values_files(chart):
                source = f"{chart.relative_to(ROOT)} with {values.relative_to(ROOT) if values else 'default values'}"
                first, second = render(chart, values), render(copy, values)
                if first.returncode or second.returncode:
                    failures.append(f"{source} failed to render: {(first.stderr or second.stderr).strip()}")
                    continue
                before, after = pod_templates(first.stdout), pod_templates(second.stdout)
                compared += len(before)
                offenders += [f"{source}: {name}" for name in template_drift(before, after)]
    for line in failures + offenders:
        print(line)
    print(
        f"{len(offenders)} pod templates change with the chart version, {compared} compared, {len(failures)} failed renders."
    )
    if offenders:
        print("A pod template carries the chart version; keep helm.sh/chart on object metadata only.")
    return 1 if offenders or failures or not compared else 0


if __name__ == "__main__":
    sys.exit(main())
