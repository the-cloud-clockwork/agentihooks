import sys
from collections.abc import Iterator
from pathlib import Path

import yaml
from chart_workers import merged


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
    volumes = {
        volume["name"]
        for volume in pod.get("volumes", [])
        if volume.get("secret", {}).get("secretName") == secret
        or any(
            source.get("secret", {}).get("name") == secret for source in volume.get("projected", {}).get("sources", [])
        )
    }
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


def main(values_paths: list[str], rendered: str) -> list[str]:
    values: dict = {}
    for path in values_paths:
        values = merged(values, yaml.safe_load(Path(path).read_text()))
    entries = values.get("controller", {}).get("secretEnv") or []
    if not entries:
        return ["controller.secretEnv names no Secret to check"]
    documents = [d for d in yaml.safe_load_all(rendered) if d]
    names = {entry["name"] for entry in entries}
    return [problem for entry in entries for problem in entry_problems(documents, entry, names - {entry["name"]})]


if __name__ == "__main__":
    found = main(sys.argv[1:], sys.stdin.read())
    for problem in found:
        print(problem, file=sys.stderr)
    if found:
        sys.exit(1)
    print("only the controller container references each controller.secretEnv Secret")
