"""Append the repository files a child process opens to the read log of the test its nearest worker runs.

Run at interpreter start by the site .pth that tests.dep_inputs writes, with CHILDREN naming the registration folder
and ROOT the repository prefix.
"""

import os
import sys
from types import FrameType

HELPERS = (f"{os.sep}pathlib.py", f"{os.sep}tokenize.py")
LOADERS = ("<frozen importlib", f"{os.sep}importlib{os.sep}", "linecache.py", "<frozen runpy>", f"{os.sep}runpy.py")


def registered_log(children: str) -> str | None:
    pid = os.getppid()
    try:
        while pid > 1:
            registration = os.path.join(children, str(pid))
            if os.path.isfile(registration):
                with open(registration) as found:
                    return found.read()
            with open(f"/proc/{pid}/stat", "rb") as stat:
                pid = int(stat.read().rpartition(b")")[2].split()[1])
    except (OSError, ValueError):
        return None
    return None


def loading(frame: FrameType | None) -> bool:
    """Whether the code that opened a file, past pathlib and tokenize helpers, is an import or traceback lookup."""
    while frame is not None and frame.f_code.co_filename.endswith(HELPERS):
        frame = frame.f_back
    return frame is not None and any(loader in frame.f_code.co_filename for loader in LOADERS)


def follow(children: str, root: str) -> None:
    if (log := registered_log(children)) is None:
        return
    descriptor = os.open(log, os.O_WRONLY | os.O_APPEND | os.O_CREAT)
    seen = set()

    def audit(event: str, args: tuple) -> None:
        if event != "open" or not isinstance(args[0], (str, bytes, os.PathLike)):
            return
        if args[1] is not None and set(str(args[1])) & set("wax"):
            return
        # An audit hook that raises fails the open it observes.
        try:
            path = os.path.abspath(os.fsdecode(args[0]))
            source = path.endswith(".py") and loading(sys._getframe(1))
            if path.startswith(root) and not path.endswith(".pyc") and not source and path not in seen:
                seen.add(path)
                os.write(descriptor, os.fsencode(path + "\n"))
        except (OSError, ValueError):
            pass

    sys.addaudithook(audit)


if __name__ == "dep_child":
    follow(CHILDREN, ROOT)  # noqa: F821
