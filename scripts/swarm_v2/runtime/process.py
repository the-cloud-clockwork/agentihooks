"""Process identity a local runtime may act on: a PID names a process only inside the PID namespace that recorded it
and only while that process keeps the start time recorded with it."""

from collections.abc import Mapping
from pathlib import Path

from hooks.proc import Process
from scripts.swarm.store import AgentRecord, SwarmError
from scripts.swarm_v2.runtime.base import Unqualified

BOOT_ID = Path("/proc/sys/kernel/random/boot_id")
PID_NAMESPACE = Path("/proc/self/ns/pid")
IDENTITY = ("process_namespace", "pid", "pid_start")
LAUNCH_LABELS = {"process_namespace": "process namespace", "pid": "process number", "pid_start": "start time"}


def local_namespace(boot_id: Path = BOOT_ID, pid_namespace: Path = PID_NAMESPACE) -> str:
    try:
        return f"{boot_id.read_text().strip()}/{pid_namespace.readlink()}"
    except OSError:
        return ""


def launch_target(pid: object, namespace: str, table: Mapping[int, Process]) -> dict:
    number = pid if type(pid) is int and pid > 0 else 0
    found = table.get(number)
    target = {"process_namespace": namespace, "pid": number, "pid_start": found.start_time if found else 0}
    missing = [LAUNCH_LABELS[field] for field in IDENTITY if not target[field]]
    if missing:
        named = f"{', '.join(missing[:-1])} and {missing[-1]}" if len(missing) > 1 else missing[0]
        raise SwarmError(f"local launch refused: its {named} could not be read")
    return target


def resolve(agent: AgentRecord, namespace: str, table: Mapping[int, Process]) -> Unqualified | int | None:
    """The recorded PID when its process still runs here with its recorded start time, None when it is gone or the
    PID was reused, or why the record cannot name a local process."""
    if not agent.execution_id:
        return Unqualified.NO_EXECUTION
    target = agent.runtime_target
    if not all(target.get(field) for field in IDENTITY):
        return Unqualified.NO_PROCESS
    if not namespace:
        return Unqualified.NO_NAMESPACE
    if target["process_namespace"] != namespace:
        return Unqualified.FOREIGN_NAMESPACE
    found = table.get(target["pid"])
    if found is None or found.start_time != target["pid_start"]:
        return None
    return found.pid
