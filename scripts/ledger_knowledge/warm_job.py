"""Refresh one ledger's knowledge index as a detached job the tick starts and never waits for."""

import fcntl
import os
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

from scripts.ledger_knowledge import cli, embedders, service
from scripts.ledger_knowledge.index import HASH


def lock_path(slug: str, environ: Mapping[str, str]) -> Path:
    """The lock file a running refresh of the ledger's hash index holds."""
    return service.index_path(slug, environ).with_suffix(".warm.lock")


def spawn(lock: int, slug: str, log: Path, environ: Mapping[str, str]) -> None:
    """Launch the refresh process, handing it the held lock."""
    with log.open("ab") as errors:
        subprocess.Popen(
            [sys.executable, "-m", "scripts.ledger_knowledge.warm_job", str(lock), slug],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=errors,
            start_new_session=True,
            pass_fds=(lock,),
            env=dict(environ),
        )


def start(slug: str, environ: Mapping[str, str]) -> bool:
    """Launch the refresh unless the semantic embedder is set or an earlier refresh still holds the lock."""
    if environ.get(embedders.PREFIX + "EMBEDDER", HASH) != HASH:
        return False
    path = lock_path(slug, environ)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        spawn(lock.fileno(), slug, path.with_suffix(".log"), environ)
    return True


def main(argv: list[str]) -> int:
    with os.fdopen(int(argv[0])):
        return cli.main(["refresh", "--ledger", argv[1]])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
