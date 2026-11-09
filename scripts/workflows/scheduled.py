"""Start one detached workflow run per scheduler delivery id; a redelivery of the same id starts nothing."""

import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from scripts.workflows import cli, store

DELIVERIES = "deliveries"
LOG = "run.log"


def delivery_folder(delivery_id: str) -> Path:
    """The folder whose exclusive creation claims a delivery id."""
    return store.default_root().parent / DELIVERIES / cli.run_folder(delivery_id).name


def command(name: str, case: Path, delivery_id: str) -> list[str]:
    """The workflow run command line for one delivery."""
    run = ["workflow", "run", name, "--case", str(case), "--run-id", delivery_id]
    return [sys.executable, "-m", "scripts.install", *run]


def start(name: str, case: str, delivery_id: str, popen: Callable[..., object] = subprocess.Popen) -> bool:
    """Spawn the run detached and return True, or False when this delivery id already started one."""
    folder = delivery_folder(delivery_id)
    folder.parent.mkdir(parents=True, exist_ok=True)
    try:
        folder.mkdir()
    except FileExistsError:
        return False
    try:
        with (folder / LOG).open("ab") as log:
            popen(
                command(name, Path(case).resolve(), delivery_id),
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                start_new_session=True,
            )
    except OSError:
        shutil.rmtree(folder)
        raise
    return True
