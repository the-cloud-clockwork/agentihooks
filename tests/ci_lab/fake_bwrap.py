"""A stand in for bubblewrap: it records its arguments and runs the command after the double dash with the same
environment, for tests of what a unit does rather than how bubblewrap isolates it."""

import sys
from pathlib import Path

SCRIPT = """import os, sys
args = sys.argv[1:]
with open({record!r}, "w") as stream:
    stream.write("".join(arg + "\\n" for arg in args))
command = args[args.index("--") + 1 :]
os.execvp(command[0], command)
"""


def install(folder: Path, record: Path) -> None:
    """Write the stand in as bwrap in folder, which the caller puts first on PATH; record gets one argument a line."""
    folder.mkdir(exist_ok=True)
    script = folder / "bwrap"
    script.write_text(f"#!{sys.executable}\n{SCRIPT.format(record=str(record))}")
    script.chmod(0o755)
