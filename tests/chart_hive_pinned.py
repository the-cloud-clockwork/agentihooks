"""Chart check: every worker hive renders its charted pin into the setting the worker start registers."""

import sys
from pathlib import Path

import yaml
from chart_hive_capabilities import EXTRA, render

START = '"pinned=${HIVE_PINNED}"'


def pinned(rendered: str, suffix: str) -> str | None:
    """The HIVE_PINNED value of the worker hive Deployment whose name ends with suffix."""
    documents = [item for item in yaml.safe_load_all(rendered) if item and item["kind"] == "Deployment"]
    pod = next(item for item in documents if item["metadata"]["name"].endswith(suffix))["spec"]["template"]["spec"]
    container = next(item for item in pod["containers"] if item["name"] == "worker-hive")
    return {item["name"]: item.get("value") for item in container.get("env", [])}.get("HIVE_PINNED")


def problems(chart: str) -> list[str]:
    """Every way the charted pin fails to reach a worker hive or its start."""
    found = []
    default = render(chart, {})
    if default.returncode:
        return [f"the default release fails to render: {default.stderr.strip()}"]
    if (declared := pinned(default.stdout, "-worker-hive")) != "no":
        found.append(f"the default hive renders HIVE_PINNED {declared!r}, not 'no'")
    charted = render(chart, {"workers": {"pinned": True}})
    if charted.returncode:
        return [*found, f"a pinned chart fails to render: {charted.stderr.strip()}"]
    if (declared := pinned(charted.stdout, "-worker-hive")) != "yes":
        found.append(f"a pinned hive renders HIVE_PINNED {declared!r}, not 'yes'")
    listed = render(chart, {"workers": {"hives": [{**EXTRA, "pinned": True}]}})
    if listed.returncode:
        return [*found, f"a pinned extra hive fails to render: {listed.stderr.strip()}"]
    if (declared := pinned(listed.stdout, "-worker-hive")) != "no":
        found.append(f"the unpinned hive beside a pinned one renders {declared!r}, not 'no'")
    if (declared := pinned(listed.stdout, "-worker-hive-b")) != "yes":
        found.append(f"the pinned extra hive renders {declared!r}, not 'yes'")
    if START not in (Path(chart).parents[1] / "worker" / "start.sh").read_text():
        found.append("the hive start does not register the charted pin")
    return found


if __name__ == "__main__":
    failures = problems(sys.argv[1])
    for line in failures:
        print(line, file=sys.stderr)
    if failures:
        sys.exit(1)
    print("the charted pin reaches every worker hive and its start")
