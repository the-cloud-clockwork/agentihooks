"""Local outbox for inbox sends, replies and closes made while Redis is unreachable, replayed in order once it returns.

A record names the item it writes, so a replay never writes twice; a full outbox refuses and never drops a record.
"""

import fcntl
import hashlib
import json
import os
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from scripts.inbox.store import Contended, DuplicateItem, InboxError, new_item_id

NAME = "inbox-outbox.jsonl"
SKIPPED = "inbox-outbox.skipped.jsonl"
LOCK = "inbox-outbox.lock"
OPS = ("send", "reply", "close", "post", "say")
MAX_ENV = "AGENTIHOOKS_INBOX_OUTBOX_MAX_BYTES"
WARN_ENV = "AGENTIHOOKS_INBOX_OUTBOX_WARN_BYTES"
DEFAULT_MAX_BYTES = 1_048_576
DEFAULT_WARN_BYTES = 786_432
QUEUED = (
    "agentihooks msg: Redis is unreachable or busy; the message is queued locally and replays when Redis returns, "
    "where its address is checked; a refused replay is reported and kept in the skipped outbox"
)


class OutboxFull(InboxError):
    pass


@dataclass
class Report:
    applied: list[str] = field(default_factory=list)
    already: list[str] = field(default_factory=list)
    rejected: list[tuple[str, str]] = field(default_factory=list)
    corrupt: int = 0
    left: int = 0


def home() -> Path:
    from hooks import config

    return Path(config.AGENTIHOOKS_HOME)


def path() -> Path:
    return home() / NAME


def skipped_path() -> Path:
    return home() / SKIPPED


def bounds(environ=None) -> tuple[int, int]:
    env = os.environ if environ is None else environ
    return int(env.get(MAX_ENV) or DEFAULT_MAX_BYTES), int(env.get(WARN_ENV) or DEFAULT_WARN_BYTES)


def checksum(body: dict) -> str:
    return hashlib.sha256(json.dumps(body).encode()).hexdigest()


def record(op: str, by: str, args: dict, item: str = "") -> dict:
    body = {"op": op, "by": by, "item": item or new_item_id(), "args": args}
    return {**body, "sum": checksum(body)}


def parse(line: str) -> dict | None:
    try:
        found = json.loads(line)
    except ValueError:
        return None
    if not isinstance(found, dict) or found.get("op") not in OPS:
        return None
    body = {key: value for key, value in found.items() if key != "sum"}
    return found if found.get("sum") == checksum(body) else None


def waiting() -> bool:
    return path().exists()


@contextmanager
def locked(blocking=True):
    home().mkdir(parents=True, exist_ok=True)
    with (home() / LOCK).open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def append(entry: dict) -> None:
    line = json.dumps(entry) + "\n"
    limit, warn = bounds()
    with locked():
        size = path().stat().st_size if path().exists() else 0
        grown = size + len(line.encode())
        if grown > limit:
            raise OutboxFull(
                f"the local inbox outbox is full ({size} of {limit} bytes, {MAX_ENV}); this message was not "
                "queued and nothing queued was dropped: bring Redis back so the outbox replays"
            )
        with path().open("a") as handle:
            handle.write(line)
            _sync(handle)
        _sync_home()
    if grown >= warn:
        print(
            f"agentihooks msg: warning: the local inbox outbox holds {grown} bytes of its {limit} byte limit",
            file=sys.stderr,
        )


def submit(store, entry: dict, apply) -> dict:
    from redis.exceptions import RedisError

    if store is not None and not waiting():
        try:
            return apply(store, entry)
        except (RedisError, Contended):
            pass
    append(entry)
    print(QUEUED, file=sys.stderr)
    return {"id": entry["item"], "op": entry["op"], "state": "queued"}


def flush(store) -> Report:
    """Replay every queued record in order; stop at the first Redis error and keep it and the rest for later."""
    report = Report()
    if not waiting():
        return report
    with locked(blocking=False) as held:
        if held:
            _replay(store, report)
    _tell(report)
    return report


def _replay(store, report: Report) -> None:
    from redis.exceptions import RedisError

    from scripts.inbox.cli import apply

    lines = path().read_text().splitlines(keepends=True)
    for index, line in enumerate(lines):
        entry = parse(line)
        if entry is None:
            _set_aside(line, "corrupt")
            report.corrupt += 1
            continue
        try:
            apply(store, entry)
            report.applied.append(entry["item"])
        except DuplicateItem:
            report.already.append(entry["item"])
        except (RedisError, Contended):
            _keep(lines[index:], report)
            return
        except (InboxError, KeyError, TypeError, ValueError) as exc:
            _set_aside(line, str(exc))
            report.rejected.append((entry["item"], str(exc)))
        except Exception as exc:
            print(f"agentihooks msg: outbox replay stopped ({type(exc).__name__}: {exc})", file=sys.stderr)
            _keep(lines[index:], report)
            return
    _clear()


def _keep(lines: list[str], report: Report) -> None:
    _rewrite(lines)
    report.left = len(lines)


def _set_aside(line: str, reason: str) -> None:
    with skipped_path().open("a") as handle:
        handle.write(json.dumps({"reason": reason, "line": line}) + "\n")
        _sync(handle)


def _clear() -> None:
    path().unlink()
    _sync_home()


def _rewrite(lines: list[str]) -> None:
    descriptor, staged = tempfile.mkstemp(dir=home())
    with os.fdopen(descriptor, "w") as handle:
        handle.write("".join(lines))
        _sync(handle)
    os.replace(staged, path())
    _sync_home()


def _sync_home() -> None:
    directory = os.open(home(), os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _sync(handle) -> None:
    handle.flush()
    os.fsync(handle.fileno())


def _tell(report: Report) -> None:
    if report.left:
        print(f"agentihooks msg: {report.left} queued messages wait in {path()} for Redis", file=sys.stderr)
    for _ in range(report.corrupt):
        print(f"agentihooks msg: a corrupt outbox record was skipped and kept in {skipped_path()}", file=sys.stderr)
    for item, reason in report.rejected:
        print(
            f"agentihooks msg: queued message {item} was refused on replay ({reason}); kept in {skipped_path()}",
            file=sys.stderr,
        )


def stable_id(*parts: str) -> str:
    """An item id fixed by what the item answers, so every retry of the same send writes it at most once."""
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()[:12]


@dataclass(frozen=True)
class Post:
    sender: str
    address: str
    text: str
    ref: str = ""
    fyi: bool = False
    task: str = ""
    item: str = ""


def post(store, message: Post):
    """A send from swarm code, written now or queued on a Redis error; the live item, or None while it waits queued."""
    args = {"address": message.address, "text": message.text, "ref": message.ref, "fyi": message.fyi}
    entry = record("post", message.sender, {**args, "task": message.task}, item=message.item)
    try:
        found = submit(store, entry, lambda live, queued: apply_post(live, queued["by"], queued))
    except DuplicateItem:
        return store.get(entry["item"])
    return None if isinstance(found, dict) else found


def apply_post(store, by: str, entry: dict):
    args = entry["args"]
    return store.send(
        by, args["address"], args["text"], ref=args["ref"], fyi=args["fyi"], task=args["task"], item_id=entry["item"]
    )
