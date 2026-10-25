"""agentihooks hive create: one hive's antoncore files from the antoncore hive template."""

from dataclasses import dataclass
from string import Template

from scripts.hive import auth

TEMPLATE_PATH = "k8s/templates/agentihooks-hive.yaml"
FILE_MARK = "#@ file: "
LAN_IP_ANNOTATION = "metallb.universe.tf/loadBalancerIPs"
SHARED_IP_ANNOTATION = "metallb.universe.tf/allow-shared-ip"
SHARED_IP_KEY = "agentihooks-swarm"
SSH_PORT_FIRST = 2222


@dataclass(frozen=True)
class Hive:
    name: str
    record: str
    ssh_port: int
    node: str
    lan_ip: str


def render(template: str, hive: Hive) -> dict[str, str]:
    try:
        text = Template(template).substitute(
            name=hive.name, record=hive.record, ssh_port=str(hive.ssh_port), node=hive.node, lan_ip=hive.lan_ip
        )
    except KeyError as exc:
        raise auth.HiveError(f"the hive template names an unknown placeholder {exc}") from None
    except ValueError as exc:
        raise auth.HiveError(f"the hive template has a malformed placeholder ({exc})") from None
    files: dict[str, list[str]] = {}
    lines = None
    for line in text.splitlines(keepends=True):
        if line.startswith(FILE_MARK):
            lines = files.setdefault(line.removeprefix(FILE_MARK).strip(), [])
        elif lines is not None:
            lines.append(line)
    if not files:
        raise auth.HiveError(f"the hive template names no file with a {FILE_MARK.strip()} line")
    return {path: "".join(body) for path, body in files.items()}


def lan_address(services: list[dict]) -> str:
    found = {
        _annotations(service).get(LAN_IP_ANNOTATION)
        for service in services
        if _annotations(service).get(SHARED_IP_ANNOTATION) == SHARED_IP_KEY
    } - {None}
    if len(found) != 1:
        raise auth.HiveError(
            f"the {SHARED_IP_KEY} LAN services name {len(found)} addresses ({', '.join(sorted(found))}); expected one"
        )
    return found.pop()


def next_port(services: list[dict], lan_ip: str) -> int:
    taken = {
        port.get("port")
        for service in services
        if lan_ip in str(_annotations(service).get(LAN_IP_ANNOTATION)).split(",")
        for port in (service.get("spec") or {}).get("ports") or []
    }
    port = SSH_PORT_FIRST
    while port in taken:
        port += 1
    return port


def kept_port(services: list[dict], name: str) -> int | None:
    for service in services:
        if (service.get("metadata") or {}).get("name") == f"{name}-ssh-lan":
            ports = (service.get("spec") or {}).get("ports") or []
            return ports[0].get("port") if ports else None
    return None


def _annotations(service: dict) -> dict:
    return (service.get("metadata") or {}).get("annotations") or {}
