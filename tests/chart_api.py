import subprocess
import sys

import yaml

DATABASE = "AGENTIHOOKS_SWARM_DATABASE_URL"
API_DATABASE = [
    "--set",
    f"api.secretEnv[0].name={DATABASE}",
    "--set",
    "api.secretEnv[0].secretName=swarm-db",
    "--set",
    "api.secretEnv[0].key=url",
]
LEDGER_DATABASE = [
    "--set",
    f"ledger.secretEnv[0].name={DATABASE}",
    "--set",
    "ledger.secretEnv[0].secretName=swarm-db",
    "--set",
    "ledger.secretEnv[0].key=url",
]
TOKEN = ["--set", "api.token.secretName=swarm-api", "--set", "api.token.key=token"]
SPLIT = ["--set", "api.enabled=true", "--set", "ledger.backend=api", *API_DATABASE, *TOKEN]


def render(chart: str, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(["helm", "template", "split", chart, *extra], capture_output=True, text=True)


def documents(text: str) -> list[dict]:
    return [document for document in yaml.safe_load_all(text) if document]


def named(found: list[dict], kind: str, suffix: str) -> list[dict]:
    return [d for d in found if d["kind"] == kind and d["metadata"]["name"].endswith(suffix)]


def container_env(workload: dict) -> dict:
    container = workload["spec"]["template"]["spec"]["containers"][0]
    return {row["name"]: row for row in container.get("env", [])}


def default_problems(chart: str) -> list[str]:
    result = render(chart)
    if result.returncode:
        return [f"the default chart fails to render: {result.stderr.strip()}"]
    found = documents(result.stdout)
    problems = []
    if named(found, "Deployment", "-api"):
        problems.append("the api deployment renders while api.enabled is off")
    ledger = named(found, "StatefulSet", "-ledger")[0]
    if "AGENTIHOOKS_SWARM_API_URL" in container_env(ledger):
        problems.append("the ledger reaches the api while its backend is direct")
    return problems


def split_problems(chart: str) -> list[str]:
    result = render(chart, *SPLIT)
    if result.returncode:
        return [f"the split chart fails to render: {result.stderr.strip()}"]
    found = documents(result.stdout)
    problems = []
    apis = named(found, "Deployment", "-api")
    if len(apis) != 1 or len(named(found, "Service", "-api")) != 1:
        return ["the split chart renders no api deployment and service"]
    api = apis[0]
    container = api["spec"]["template"]["spec"]["containers"][0]
    if container.get("command") != ["python", "-m", "scripts.swarm_ledger.api.store"]:
        problems.append(f"the api runs {container.get('command')}")
    api_env = container_env(api)
    if DATABASE not in api_env or "AGENTIHOOKS_SWARM_API_TOKEN" not in api_env:
        problems.append("the api holds no database URL or no token")
    ledger_env = container_env(named(found, "StatefulSet", "-ledger")[0])
    if DATABASE in ledger_env:
        problems.append("the ledger still holds the database URL on the api backend")
    if ledger_env.get("AGENTIHOOKS_SWARM_API_URL", {}).get("value") != "http://split-agentihooks-swarm-api:8766":
        problems.append(f"the ledger api address is {ledger_env.get('AGENTIHOOKS_SWARM_API_URL')}")
    if "AGENTIHOOKS_SWARM_API_TOKEN" not in ledger_env:
        problems.append("the ledger sends no api token")
    return problems


def refusal_problems(chart: str) -> list[str]:
    cases = {
        "an api without the database URL": ["--set", "api.enabled=true"],
        "an api backend without the api": ["--set", "ledger.backend=api"],
        "a database URL on a ledger that uses the api": [*SPLIT, *LEDGER_DATABASE],
    }
    return [f"the chart renders {name}" for name, extra in cases.items() if render(chart, *extra).returncode == 0]


def main(chart: str) -> int:
    problems = default_problems(chart) + split_problems(chart) + refusal_problems(chart)
    for problem in problems:
        print(problem, file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
