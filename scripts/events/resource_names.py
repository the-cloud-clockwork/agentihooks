import hashlib
import re

COMPONENT = re.compile(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*")
QUEUE_PREFIX = "AGENTIHOOKS-QUEUE-"


def consumer_name(value: str) -> str:
    if len(value) <= 64 and COMPONENT.fullmatch(value):
        return value
    return "h" + hashlib.sha256(value.encode()).hexdigest()


def queue_stream(name: str) -> str:
    return QUEUE_PREFIX + consumer_name(name)


def legacy_queue_stream(name: str) -> str:
    suffix = name if re.fullmatch("[A-Za-z0-9_-]{1,64}", name) else "h" + hashlib.sha256(name.encode()).hexdigest()
    return f"AGENTIHOOKS_QUEUE_{suffix}"


def legacy_stream_name(swarm: str, resource: str) -> str:
    return f"AGENTIHOOKS_{swarm.encode().hex()}_{resource}"
