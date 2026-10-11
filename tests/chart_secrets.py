import sys
from collections.abc import Iterator
from pathlib import Path

import yaml
from chart_workers import merged

CA_DIR = "/run/secrets/terminal-ca"
CA_ENV = "AGENTIHOOKS_TERMINAL_CA_KEY"
CA_VOLUME = "terminal-ca"


def pods(node: object) -> Iterator[dict]:
    if isinstance(node, dict):
        if isinstance(node.get("containers"), list):
            yield node
        for value in node.values():
            yield from pods(value)
    elif isinstance(node, list):
        for value in node:
            yield from pods(value)


def containers(documents: list) -> Iterator[tuple[dict, dict, dict]]:
    for document in documents:
        for pod in pods(document.get("spec", {})):
            for container in pod.get("initContainers", []) + pod.get("containers", []):
                yield document, pod, container


def references(pod: dict, container: dict, secret: str) -> list[dict]:
    found = [
        item
        for item in container.get("env", [])
        if item.get("valueFrom", {}).get("secretKeyRef", {}).get("name") == secret
    ]
    found += [item for item in container.get("envFrom", []) if item.get("secretRef", {}).get("name") == secret]
    volumes = {volume["name"] for volume in pod.get("volumes", []) if holds(volume, secret)}
    found += [mount for mount in container.get("volumeMounts", []) if mount["name"] in volumes]
    return found


def expected_reference(entry: dict) -> dict:
    ref = {"name": entry["secretName"], "key": entry["key"]}
    if entry.get("optional"):
        ref["optional"] = True
    return {"name": entry["name"], "valueFrom": {"secretKeyRef": ref}}


def is_controller(document: dict, container: dict) -> bool:
    return (
        document["kind"] == "Deployment"
        and document["metadata"]["name"].endswith("-controller")
        and container["name"] == "controller"
    )


def entry_problems(documents: list, entry: dict, others: set[str]) -> list[str]:
    secret, held, problems = entry["secretName"], [], []
    for document, pod, container in containers(documents):
        found = references(pod, container, secret)
        if found and is_controller(document, container):
            held += [item for item in found if item.get("name") not in others]
        elif found:
            owner = f"{document['kind']} {document['metadata']['name']} container {container['name']}"
            problems.append(f"{owner} references the controller only Secret {secret}")
    if held != [expected_reference(entry)]:
        problems.append(
            f"the controller container references {secret} as {held}, expected {[expected_reference(entry)]}"
        )
    return problems


def holds(volume: dict, secret: str) -> bool:
    return volume.get("secret", {}).get("secretName") == secret or any(
        source.get("secret", {}).get("name") == secret for source in volume.get("projected", {}).get("sources", [])
    )


def pod_volumes(pod: dict, secret: str) -> list[dict]:
    return [volume for volume in pod.get("volumes", []) if holds(volume, secret)]


def terminal_ca_problems(documents: list, ca: dict) -> list[str]:
    secret, problems, held, volumes, setting = ca["secretName"], [], [], [], []
    for document in documents:
        for pod in pods(document.get("spec", {})):
            owner = f"{document['kind']} {document['metadata']['name']}"
            found = pod_volumes(pod, secret)
            if not (found and any(is_controller(document, c) for c in pod.get("containers", []))):
                problems += [f"{owner} holds a volume of the terminal CA Secret {secret}"] if found else []
                continue
            volumes += found
    for document, pod, container in containers(documents):
        found = references(pod, container, secret)
        if is_controller(document, container):
            held += found
            setting += [item for item in container.get("env", []) if item["name"] == CA_ENV]
        elif found:
            owner = f"{document['kind']} {document['metadata']['name']} container {container['name']}"
            problems.append(f"{owner} references the terminal CA Secret {secret}")
    path = f"{CA_DIR}/{ca['key']}"
    expected = {
        "name": CA_VOLUME,
        "secret": {"secretName": secret, "defaultMode": 0o400, "items": [{"key": ca["key"], "path": ca["key"]}]},
    }
    if volumes != [expected]:
        problems.append(f"the controller Pod holds {secret} as {volumes}, expected {[expected]}")
    if held != [{"name": CA_VOLUME, "mountPath": CA_DIR, "readOnly": True}]:
        problems.append(f"the controller container references {secret} as {held}, expected one read only mount")
    if setting != [{"name": CA_ENV, "value": path}]:
        problems.append(f"the controller container sets {CA_ENV} as {setting}, expected {path}")
    return problems


def main(values_paths: list[str], rendered: str) -> list[str]:
    values: dict = {}
    for path in values_paths:
        values = merged(values, yaml.safe_load(Path(path).read_text()))
    entries = values.get("controller", {}).get("secretEnv") or []
    ca = values.get("controller", {}).get("terminalCa") or {}
    if not entries and not ca.get("secretName"):
        return ["controller.secretEnv and controller.terminalCa name no Secret to check"]
    documents = [d for d in yaml.safe_load_all(rendered) if d]
    names = {entry["name"] for entry in entries}
    problems = [problem for entry in entries for problem in entry_problems(documents, entry, names - {entry["name"]})]
    return problems + (terminal_ca_problems(documents, ca) if ca.get("secretName") else [])


if __name__ == "__main__":
    found = main(sys.argv[1:], sys.stdin.read())
    for problem in found:
        print(problem, file=sys.stderr)
    if found:
        sys.exit(1)
    print("only the controller container references each Secret reserved for it")
