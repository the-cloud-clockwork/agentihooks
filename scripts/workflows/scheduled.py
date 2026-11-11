"""Start one detached workflow run per scheduler delivery id; a redelivery of the same id starts nothing."""

import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from scripts.workflows import cli, machine, store

if TYPE_CHECKING:
    from scripts.inbox.store import InboxStore

DELIVERIES = "deliveries"
LOG = "run.log"
SENDER = "scheduler"
FAILED = "failed"


def delivery_folder(delivery_id: str) -> Path:
    """The folder whose exclusive creation claims a delivery id."""
    return store.default_root().parent / DELIVERIES / cli.run_folder(delivery_id).name


def command(name: str, case: Path, delivery_id: str, owner: str) -> list[str]:
    """The detached process line that runs one delivery and reports it to the owner."""
    return [sys.executable, "-m", "scripts.workflows.scheduled", owner, name, str(case), delivery_id]


def start(
    name: str,
    case: str,
    project: str,
    delivery_id: str,
    owner: str,
    popen: Callable[..., object] = subprocess.Popen,
) -> bool:
    """Spawn the run detached in its scheduling project and return True, or False when this delivery id already started one."""
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
                cwd=project,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                start_new_session=True,
            )
    except OSError:
        shutil.rmtree(folder)
        raise
    return True


def run_status(delivery_id: str) -> str:
    """The run's saved status; a run that saved no readable status or stopped mid run reads failed."""
    try:
        folder = cli.run_folder(delivery_id)
        state = store.load(folder)  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        found = str(state["status"])  # pyright: ignore[reportUnknownArgumentType]
    except (OSError, ValueError, KeyError):
        return FAILED
    return FAILED if found == machine.RUNNING else found


def connect_inbox() -> "InboxStore | None":
    """The swarm inbox, or None while Redis is unreachable so the notice waits in the outbox."""
    from scripts.inbox import store as inbox_store

    try:
        return inbox_store.connect()
    except inbox_store.Unreachable:
        return None


def notify(owner: str, delivery_id: str, inbox: "InboxStore | None" = None) -> None:
    """Send the owner the run's status and folder, queued while Redis is down; a resend is dropped."""
    from scripts.inbox import outbox

    text = f"workflow run {delivery_id} {run_status(delivery_id)}, run folder {cli.run_folder(delivery_id)}"
    notice = outbox.Post(SENDER, owner, text, item=f"{delivery_id}:done")
    outbox.post(inbox or connect_inbox(), notice)  # pyright: ignore[reportUnknownMemberType]


def main(argv: list[str]) -> int:
    """Run one scheduled delivery in this process, then always report its result to the owner."""
    owner, name, case, delivery_id = argv
    try:
        return cli.main(["run", name, "--case", case, "--run-id", delivery_id])
    finally:
        notify(owner, delivery_id)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
