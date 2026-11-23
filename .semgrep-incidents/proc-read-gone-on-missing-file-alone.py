from pathlib import Path


def missing_alone(pid: int) -> bool:
    try:
        # ruleid: proc-read-gone-on-missing-file-alone
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except FileNotFoundError:
        return True
    return state == "Z"


def reaped_too(pid: int) -> bool:
    try:
        # ok: proc-read-gone-on-missing-file-alone
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except (FileNotFoundError, ProcessLookupError):
        return True
    return state == "Z"


def any_os_error(pid: int) -> str:
    try:
        # ok: proc-read-gone-on-missing-file-alone
        return Path(f"/proc/{pid}/comm").read_text()
    except OSError:
        return ""
