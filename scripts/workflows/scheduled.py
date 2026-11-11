"""Start one detached workflow run per scheduler delivery id; a redelivery of the same id starts nothing."""

import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from scripts.workflows import cli, store

if TYPE_CHECKING:
    from scripts.inbox.store import InboxStore

DELIVERIES = "deliveries"
LOG = "run.log"
SENDER = "scheduler"


def delivery_folder(delivery_id: str) -> Path:
    """The folder whose exclusive creation claims a delivery id."""
    return store.default_root().parent / DELIVERIES / cli.run_folder(delivery_id).name


def command(name: str, case: Path, delivery_id: str, owner: str) -> list[str]:
    """The detached process line that runs one delivery and reports it to the owner."""
    return [sys.executable, "-m", "scripts.workflows.scheduled", owner, name, str(case), delivery_id]


def start(name: str, case: str, delivery_id: str, owner: str, popen: Callable[..., object] = subprocess.Popen) -> bool:
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
                command(name, Path(case).resolve(), delivery_id, owner),
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                start_new_session=True,
            )
    except OSError:
        shutil.rmtree(folder)
        raise
    return True


def notify(owner: str, delivery_id: str, inbox: "InboxStore | None" = None) -> None:
    """Send the owner one inbox item naming the run status and folder; a resent notice is dropped."""
    from scripts.inbox import store as inbox_store

    folder = cli.run_folder(delivery_id)
    try:
        status = store.load(folder)["status"]
    except (OSError, ValueError, KeyError):
        status = "failed"
    text = f"workflow run {delivery_id} {status}, run folder {folder}"
    try:
        (inbox or inbox_store.connect()).send(SENDER, owner, text, item_id=f"{delivery_id}:done")
    except inbox_store.DuplicateItem:
        pass


def main(argv: list[str]) -> int:
    """Run one scheduled delivery in this process, then always report its result to the owner."""
    owner, name, case, delivery_id = argv
    try:
        return cli.main(["run", name, "--case", case, "--run-id", delivery_id])
    finally:
        notify(owner, delivery_id)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
