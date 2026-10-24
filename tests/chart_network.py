import json
import sys
from pathlib import Path

import yaml
from chart_workers import find, merged


def peers_problems(policy: dict, expected: list, label: str) -> list[str]:
    rules = policy["spec"].get("ingress", [])
    if len(rules) != 1:
        return [f"the {label} policy has {len(rules)} ingress rules, expected one"]
    found = rules[0].get("from", [])
    problems = [
        f"the {label} policy admits {json.dumps(p, sort_keys=True)} beyond its peers"
        for p in found
        if p not in expected
    ]
    problems += [
        f"the {label} policy does not admit {json.dumps(p, sort_keys=True)}" for p in expected if p not in found
    ]
    return problems


def main(release: str, values_paths: list[str], rendered: str) -> list[str]:
    values: dict = {}
    for path in values_paths:
        values = merged(values, yaml.safe_load(Path(path).read_text()))
    documents = [d for d in yaml.safe_load_all(rendered) if d]
    api = find(documents, "NetworkPolicy", "-controller-api")
    ledger = find(documents, "NetworkPolicy", "-ledger")
    controller = {
        "podSelector": {
            "matchLabels": {
                "app.kubernetes.io/name": "agentihooks-swarm",
                "app.kubernetes.io/instance": release,
                "app.kubernetes.io/component": "controller",
            }
        }
    }
    problems = peers_problems(api, [controller, *values["controller"]["api"]["ingressFrom"]], "controller API")
    if api["spec"]["ingress"][0].get("ports") != [{"port": "api"}]:
        problems.append("the controller API policy admits ports other than api")
    in_release = {"podSelector": {"matchLabels": {"app.kubernetes.io/instance": release}}}
    problems += peers_problems(ledger, [in_release, *values["networkPolicy"]["ingressFrom"]], "ledger")
    return problems


if __name__ == "__main__":
    found = main(sys.argv[1], sys.argv[2:], sys.stdin.read())
    for problem in found:
        print(problem, file=sys.stderr)
    if found:
        sys.exit(1)
    print("the controller API policy admits only the release and controller.api.ingressFrom")
