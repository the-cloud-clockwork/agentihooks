import subprocess
import sys

import yaml

LEDGER = "agentihooks-swarm-ledger"
SHARED = [
    "--set",
    "ledger.secretEnv[0].name=AGENTIHOOKS_SWARM_DATABASE_URL",
    "--set",
    "ledger.secretEnv[0].secretName=swarm-postgres",
    "--set",
    "ledger.secretEnv[0].key=url",
]
SHARED_ENV = ["--set", "ledger.env.AGENTIHOOKS_SWARM_DATABASE_URL=postgresql://ledger@db/ledger"]
EMPTY_ENV = ["--set", "ledger.env.AGENTIHOOKS_SWARM_DATABASE_URL="]
REFUSAL = "ledger.replicas above 1 needs the shared database"


def render(chart: str, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(["helm", "template", "agentihooks-swarm", chart, *extra], capture_output=True, text=True)


def ledger_docs(text: str) -> dict:
    return {
        d["kind"]: d
        for d in yaml.safe_load_all(text)
        if d and d["metadata"]["name"] == LEDGER and d["kind"] in {"StatefulSet", "PodDisruptionBudget"}
    }


def refused(chart: str, label: str, refusal: str, *extra: str) -> list[str]:
    result = render(chart, *extra)
    if result.returncode == 0 or refusal not in result.stderr:
        return [f"{label} renders instead of failing with {refusal!r}: {result.stderr.strip()}"]
    return []


def surge_problems(chart: str, label: str, *extra: str) -> list[str]:
    result = render(chart, "--set", "ledger.replicas=2", *extra)
    if result.returncode:
        return [f"{label} fails to render two ledger replicas: {result.stderr.strip()}"]
    docs = ledger_docs(result.stdout)
    if "StatefulSet" not in docs:
        return [f"{label} renders no ledger StatefulSet named {LEDGER}"]
    found = []
    workload = docs["StatefulSet"]["spec"]
    if workload["replicas"] != 2:
        found.append(f"{label} renders {workload['replicas']} ledger replicas, expected 2")
    if workload.get("minReadySeconds", 0) < 5:
        found.append(f"{label} lets a new ledger replica count as available before it has served five seconds")
    container = workload["template"]["spec"]["containers"][0]
    pause = container.get("lifecycle", {}).get("preStop", {}).get("exec", {}).get("command", [])
    if "sleep" not in " ".join(pause):
        found.append(f"{label} stops a ledger server before the Service drops it: no preStop pause")
    if workload["template"]["spec"].get("terminationGracePeriodSeconds", 0) < 20:
        found.append(f"{label} kills a ledger server before the preStop pause and its drain end")
    budget = docs.get("PodDisruptionBudget", {}).get("spec", {})
    if budget.get("maxUnavailable") != 1:
        found.append(f"{label} renders no disruption budget that keeps all but one ledger replica up: {budget}")
    return found


def problems(chart: str) -> list[str]:
    found = [
        *refused(chart, "two folder ledgers", REFUSAL, "--set", "ledger.replicas=2"),
        *refused(chart, "zero ledgers", "ledger.replicas must be at least 1", "--set", "ledger.replicas=0"),
        *surge_problems(chart, "a ledger on the shared database secret", *SHARED),
        *surge_problems(chart, "a ledger on the shared database env", *SHARED_ENV),
        *refused(chart, "an empty shared database env", REFUSAL, "--set", "ledger.replicas=2", *EMPTY_ENV),
    ]
    single = render(chart, *SHARED)
    if single.returncode:
        return [*found, f"one ledger on the shared database fails to render: {single.stderr.strip()}"]
    if "PodDisruptionBudget" in ledger_docs(single.stdout):
        found.append("one ledger replica renders a disruption budget that would block every node drain")
    return found


if __name__ == "__main__":
    failures = problems(sys.argv[1])
    for line in failures:
        print(line, file=sys.stderr)
    if failures:
        sys.exit(1)
    print("the chart runs several ledger replicas only on the shared database and keeps one serving through a rollout")
