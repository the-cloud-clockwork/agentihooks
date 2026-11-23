from pathlib import Path


def missing_alone(pid: int) -> bool:
    try:
        # ruleid: proc-read-gone-on-missing-file-alone
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except FileNotFoundError:
        return True
    return state == "Z"


def path_built_before_the_try(pid: int) -> bool:
    status = Path(f"/proc/{pid}/status")
    for _ in range(3):
        try:
            # ruleid: proc-read-gone-on-missing-file-alone
            if "State:\tZ" in status.read_text():
                return True
        except FileNotFoundError:
            return True
    return False


def percent_format(pid: int) -> str:
    try:
        # ruleid: proc-read-gone-on-missing-file-alone
        with open("/proc/%d/comm" % pid) as handle:
            return handle.read()
    except FileNotFoundError:
        return ""


def joined_text(pid: int) -> str:
    try:
        # ruleid: proc-read-gone-on-missing-file-alone
        with open("/proc/" + str(pid) + "/comm") as handle:
            return handle.read()
    except FileNotFoundError:
        return ""


def joined_name(name: str) -> str:
    try:
        # ok: proc-read-gone-on-missing-file-alone
        with open("/proc/" + name) as handle:
            return handle.read()
    except FileNotFoundError:
        return ""


def reaped_too(pid: int) -> bool:
    try:
        # ok: proc-read-gone-on-missing-file-alone
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except (FileNotFoundError, ProcessLookupError):
        return True
    return state == "Z"


def any_os_error(pid: int) -> str:
    comm = Path(f"/proc/{pid}/comm")
    try:
        # ok: proc-read-gone-on-missing-file-alone
        return comm.read_text()
    except OSError:
        return ""
