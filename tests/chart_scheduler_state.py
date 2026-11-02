import subprocess
import sys

import yaml

SETTING = "AGENTIHOOKS_SCHEDULER_STATE_BACKEND"


def render(chart: str, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(["helm", "template", "scheduler", chart, *extra], capture_output=True, text=True)


def controller_env(text: str) -> dict:
    documents = [d for d in yaml.safe_load_all(text) if d]
    deployment = next(
        d for d in documents if d["kind"] == "Deployment" and d["metadata"]["name"].endswith("-controller")
    )
    container = next(c for c in deployment["spec"]["template"]["spec"]["containers"] if c["name"] == "controller")
    return {row["name"]: row.get("value") for row in container.get("env", [])}


def problems(chart: str) -> list[str]:
    found = []
    for setting, expected in ((None, None), ("redis", "redis"), ("bus", "bus")):
        options = [] if setting is None else ["--set", f"controller.schedulerState={setting}"]
        result = render(chart, *options)
        if result.returncode:
            found.append(f"controller.schedulerState {setting} fails to render: {result.stderr.strip()}")
            continue
        chosen = controller_env(result.stdout).get(SETTING)
        if chosen != expected:
            found.append(f"controller.schedulerState {setting} renders {SETTING}={chosen}, expected {expected}")
    refused = render(chart, "--set", "controller.schedulerState=nats")
    if refused.returncode == 0 or "controller.schedulerState must be redis or bus" not in refused.stderr:
        found.append("controller.schedulerState nats renders instead of failing on the unknown backend")
    return found


if __name__ == "__main__":
    failures = problems(sys.argv[1])
    for line in failures:
        print(line, file=sys.stderr)
    if failures:
        sys.exit(1)
    print("the controller renders the scheduler state backend named by controller.schedulerState and refuses others")
