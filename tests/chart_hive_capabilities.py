"""Chart check: every worker hive renders its charted capabilities into the setting the worker start registers."""

import subprocess
import sys
from pathlib import Path

import yaml

HIVE = ["--set", "workers.replicas=1", "--set", "workers.credentialSecret.name=m"]
HIVE += ["--set", "workers.credentialSecret.key=c"]
EXTRA = {"name": "b", "credentialSecret": {"name": "member-b", "key": "credential"}, "capabilities": ["gpu"]}
START = '"capabilities=${HIVE_CAPABILITIES}"'
REFUSAL = "each workers.capabilities entry must be a capability name"


def render(chart: str, values: dict) -> subprocess.CompletedProcess[str]:
    """Renders the chart with one worker hive and the given values."""
    return subprocess.run(
        ["helm", "template", "fx", chart, *HIVE, "-f", "-"],
        input=yaml.safe_dump(values),
        capture_output=True,
        text=True,
    )


def capabilities(rendered: str, suffix: str) -> str | None:
    """The HIVE_CAPABILITIES value of the worker hive Deployment whose name ends with suffix."""
    documents = [item for item in yaml.safe_load_all(rendered) if item and item["kind"] == "Deployment"]
    pod = next(item for item in documents if item["metadata"]["name"].endswith(suffix))["spec"]["template"]["spec"]
    container = next(item for item in pod["containers"] if item["name"] == "worker-hive")
    return {item["name"]: item.get("value") for item in container.get("env", [])}.get("HIVE_CAPABILITIES")


def problems(chart: str) -> list[str]:
    """Every way the charted capabilities fail to reach a worker hive or its start."""
    found = []
    default = render(chart, {})
    if default.returncode:
        return [f"the default release fails to render: {default.stderr.strip()}"]
    if (declared := capabilities(default.stdout, "-worker-hive")) != "":
        found.append(f"the default hive declares {declared!r}, not an empty list")
    listed = render(chart, {"workers": {"capabilities": ["anton", "arm"], "hives": [EXTRA]}})
    if listed.returncode:
        return [*found, f"charted capabilities fail to render: {listed.stderr.strip()}"]
    if (declared := capabilities(listed.stdout, "-worker-hive")) != "anton,arm":
        found.append(f"the hive declares {declared!r}, not 'anton,arm'")
    if (declared := capabilities(listed.stdout, "-worker-hive-b")) != "gpu":
        found.append(f"the extra hive declares {declared!r}, not 'gpu'")
    refused = render(chart, {"workers": {"capabilities": ["a,b"]}})
    if refused.returncode == 0 or REFUSAL not in refused.stderr:
        found.append(f"a capability holding a comma is not refused: {refused.stderr.strip()}")
    if START not in (Path(chart).parents[1] / "worker" / "start.sh").read_text():
        found.append("the hive start does not register the charted capabilities")
    return found


if __name__ == "__main__":
    failures = problems(sys.argv[1])
    for line in failures:
        print(line, file=sys.stderr)
    if failures:
        sys.exit(1)
    print("charted capabilities reach every worker hive and its start")
