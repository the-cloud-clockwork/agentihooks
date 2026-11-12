"""Start one detached workflow run per scheduler delivery id; a redelivery of the same id starts nothing."""

import json
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from scripts.workflows import cli, machine, store

if TYPE_CHECKING:
    from scripts.inbox.store import InboxStore

DELIVERIES = "deliveries"
LOG = "run.log"
SENDER = "scheduler"
FAILED = "failed"
RESULT = "result"
NOTICED = "noticed"


class Receipts(Protocol):
    def hget(self, name: str, key: str) -> Any: ...

    def hsetnx(self, name: str, key: str, value: str) -> Any: ...


def delivery_folder(delivery_id: str) -> Path:
    """The folder whose exclusive creation claims a delivery id."""
    return store.default_root().parent / DELIVERIES / cli.run_folder(delivery_id).name


def command(name: str, case: Path, delivery_id: str, owner: str, hive: str) -> list[str]:
    """The detached process line that runs one delivery and reports it to the owner, or to the receipt on a hive."""
    line = [sys.executable, "-P", "-m", "scripts.workflows.scheduled", owner, name, str(case), delivery_id]
    return [*line, hive] if hive else line


def start(
    name: str,
    case: str,
    project: str,
    delivery_id: str,
    owner: str,
    popen: Callable[..., object] = subprocess.Popen,
    hive: str = "",
) -> bool:
    """Spawn the run detached in its scheduling project; return False when this delivery id already started one."""
    folder = delivery_folder(delivery_id)
    folder.parent.mkdir(parents=True, exist_ok=True)
    try:
        folder.mkdir()
    except FileExistsError:
        return False
    try:
        with (folder / LOG).open("ab") as log:
            popen(
                command(name, Path(case).resolve(), delivery_id, owner, hive),
                cwd=project or None,
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


def receipt(hive: str, run_id: str) -> str:
    """The run order receipt the hive acks on, which also carries the run's result."""
    from scripts.hive import registry

    return f"{registry.key(hive)}:run-order:{run_id}"


def report(hive: str, run_id: str, redis: Receipts | None = None) -> None:
    """Record the run's final status and run folder files on its receipt once; a later report keeps the first."""
    from scripts.swarm.store import redis_client  # pyright: ignore[reportUnknownVariableType]

    folder = cli.run_folder(run_id)
    saved = store.files(folder) if folder.is_dir() else {}
    result = {"status": run_status(run_id), "hive": hive, "files": saved}
    client: Receipts = redis or redis_client()  # pyright: ignore[reportUnknownVariableType]
    client.hsetnx(receipt(hive, run_id), RESULT, json.dumps(result))


def collect(redis: Receipts, hive: str, run_id: str, owner: str, inbox: "InboxStore | None" = None) -> bool:
    """On the sending host, restore a reported hive run's folder and post its owner one notice naming the hive."""
    from scripts.inbox import outbox

    raw = redis.hget(receipt(hive, run_id), RESULT)
    if not raw:
        return False
    result = json.loads(str(raw))
    folder = cli.run_folder(run_id)
    store.restore(folder, result["files"])
    if redis.hget(receipt(hive, run_id), NOTICED):
        return False
    text = f"workflow run {run_id} {result['status']} on hive {hive}, run folder {folder}"
    notice = outbox.Post(SENDER, owner, text, item=f"{run_id}:done")
    outbox.post(inbox or connect_inbox(), notice)  # pyright: ignore[reportUnknownMemberType]
    return bool(redis.hsetnx(receipt(hive, run_id), NOTICED, "1"))


def main(argv: list[str]) -> int:
    """Run one scheduled delivery in this process, then always report its result: to the receipt on a hive, else to
    the owner."""
    owner, name, case, delivery_id, *placed = argv
    try:
        return cli.main(["run", name, "--case", case, "--run-id", delivery_id])
    finally:
        if placed:
            report(placed[0], delivery_id)
        else:
            notify(owner, delivery_id)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
