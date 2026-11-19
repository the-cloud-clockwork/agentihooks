import socket
from typing import Any

from scripts.codex_session import supervisor


def unguarded(record: Any) -> bool:
    # ruleid: managed-process-without-owner-check
    return supervisor.owns(record, supervisor.start_time)


def guarded(record: Any) -> bool | None:
    if record.host_id and record.host_id != socket.gethostname():
        return None
    # ok: managed-process-without-owner-check
    return supervisor.owns(record, supervisor.start_time)
