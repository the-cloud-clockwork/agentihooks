import sys
from pathlib import Path

import yaml
from chart_secrets import containers, references

URL_ENV = "AGENTIHOOKS_SWARM_REDIS_URL"
ROLES = {
    ("StatefulSet", "-ledger"): "ledger",
    ("Deployment", "-controller"): "controller",
    ("Deployment", "-worker"): "worker",
}


def role_of(document: dict) -> str | None:
    for (kind, suffix), role in ROLES.items():
        if document["kind"] == kind and document["metadata"]["name"].endswith(suffix):
            return role
    return None


def url_reference(container: dict) -> dict | None:
    for item in container.get("env", []):
        if item["name"] == URL_ENV:
            return item.get("valueFrom", {}).get("secretKeyRef", {"value": item.get("value")})
    return None


def problems(documents: list, redis: dict) -> list[str]:
    admin, roles, found = redis["existingSecret"]["name"], redis["roles"], []
    for document, pod, container in containers(documents):
        role = role_of(document)
        where = f"{document['kind']} {document['metadata']['name']} container {container['name']}"
        if references(pod, container, admin) and not document["metadata"]["name"].endswith("-hive-join"):
            found.append(f"{where} references the administrator Redis Secret {admin}")
        reference = url_reference(container)
        if role is None or reference is None:
            continue
        expected = {"name": roles[role]["name"], "key": roles[role]["key"]}
        if reference != expected:
            found.append(f"{where} reads the Redis URL from {reference}, not the {role} Secret {expected}")
    return found


def main() -> int:
    documents = [document for document in yaml.safe_load_all(sys.stdin) if document]
    redis = yaml.safe_load(Path(sys.argv[1]).read_text())["redis"]
    found = problems(documents, redis)
    for problem in found:
        print(problem, file=sys.stderr)
    if not any(role_of(document) for document in documents):
        print("the rendered chart holds no ledger, controller or worker", file=sys.stderr)
        return 1
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
