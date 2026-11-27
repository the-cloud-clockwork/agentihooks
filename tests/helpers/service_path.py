"""Runs a service's agentihooks command under the default systemd PATH, with no folder that holds agentihooks."""

import os
from pathlib import Path
from subprocess import CompletedProcess, run

SYSTEMD_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/usr/games:/usr/local/games:/snap/bin"


def service_path() -> str:
    """The default systemd PATH less every folder that holds an agentihooks executable."""
    return os.pathsep.join(folder for folder in SYSTEMD_PATH.split(":") if not (Path(folder) / "agentihooks").exists())


def help_run(command: list[str], folder: Path) -> CompletedProcess[str]:
    """Run the command with --help from a folder outside the repository, under the service PATH."""
    return run(
        [*command, "--help"],
        cwd=folder,
        env={**os.environ, "PATH": service_path()},
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
