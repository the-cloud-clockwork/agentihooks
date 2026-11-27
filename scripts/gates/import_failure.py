"""A gate whose implementation cannot be imported: the call goes through, every failure writes a fail-open row naming
the gate, the import error and the interpreter, and one setup alert per gate per hour tells the swarm which gate is off.

The hour is claimed across processes in one stamp file under the swarm's gates directory, held by a file lock while it
is read and rewritten; stamps an hour old or older are dropped on each claim, and a send that fails frees its claim so
the next failure retries.
"""

import json
import os
import sys
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from scripts.gates import log
from scripts.gates.base import Who
from scripts.platform_compat import file_lock

HOUR_MS = 3_600_000
TIMEOUT = 2.0
STAMPS = "import-alerts.json"
ITEM = "gate-import-"
SEND_ERRORS = (ImportError, LookupError, OSError, RuntimeError, SystemExit, ValueError)


@dataclass(frozen=True)
class Failure:
    """One gate that could not be imported: its registered name, the import error and the interpreter that hit it."""

    gate: str
    error: str
    interpreter: str
    tool: str = ""

    def reason(self) -> str:
        return f"import failed under {self.interpreter}: {self.error}"

    def text(self) -> str:
        return (
            f"The {self.gate} gate cannot import its implementation under {self.interpreter} and lets every call "
            f"through until that interpreter is repaired: {self.error}"
        )


def report(failure: Failure, environ: Mapping[str, str] | None = None, home: Path | str | None = None) -> None:
    """Write the fail-open row of a swarm member's failed gate import and send the gate's alert when its hour is free."""
    who: Who = Who.from_env(environ)  # pyright: ignore[reportUnknownMemberType]
    if not who.swarm:
        return
    now_ms = int(time.time() * 1000)
    try:
        row = log.Row.of(failure.gate, "fail-open", who, failure.tool, failure.reason(), now_ms)  # pyright: ignore[reportUnknownMemberType]
        log.append(who.swarm, row, home)  # pyright: ignore[reportUnknownMemberType]
    except OSError as exc:
        _note("gate log row not written", failure, exc)
    alert(who, failure, home, now_ms)


def alert(who: Who, failure: Failure, home: Path | str | None, now_ms: int) -> None:
    """Send the gate's setup alert unless its hour is already claimed; a send that fails frees the claim."""
    if not _claimed(who.swarm, failure, home, now_ms):
        return
    try:
        _send(who, failure)
    except SEND_ERRORS as exc:
        _note("alert not sent", failure, exc)
        _free(who.swarm, failure, home, now_ms)


def claim(slug: str, gate: str, now_ms: int, home: Path | str | None = None) -> bool:
    """Take the gate's alert hour from now_ms unless a stamp younger than an hour holds it."""
    path = _stamps(slug, home)
    with file_lock(path.with_suffix(".lock")):
        stamps = {name: at for name, at in _read(path).items() if now_ms - at < HOUR_MS}
        taken = gate not in stamps
        if taken:
            stamps[gate] = now_ms
        _write(path, stamps)
    return taken


def release(slug: str, gate: str, stamp_ms: int, home: Path | str | None = None) -> None:
    """Drop the gate's stamp when it is still the one stamp_ms claimed."""
    path = _stamps(slug, home)
    with file_lock(path.with_suffix(".lock")):
        stamps = _read(path)
        if stamps.get(gate) == stamp_ms:
            del stamps[gate]
            _write(path, stamps)


def _claimed(slug: str, failure: Failure, home: Path | str | None, now_ms: int) -> bool:
    try:
        return claim(slug, failure.gate, now_ms, home)
    except OSError as exc:
        _note("alert hour not claimed", failure, exc)
        return False


def _free(slug: str, failure: Failure, home: Path | str | None, now_ms: int) -> None:
    try:
        release(slug, failure.gate, now_ms, home)
    except OSError as exc:
        _note("alert hour not freed", failure, exc)


def _send(who: Who, failure: Failure) -> None:
    from scripts.swarm_ledger import ledger, ledger_alerts

    op = {
        "op": "alert_raise",
        "id": f"alert_raise-{uuid.uuid4().hex[:10]}",
        "by": who.name or "swarm",
        "source": ledger_alerts.SETUP,
        "item": ITEM + failure.gate,
        "text": failure.text()[: ledger_alerts.MAX_OUTCOME],
    }
    ledger.client(who.swarm, TIMEOUT).send(who.swarm, [op])


def _stamps(slug: str, home: Path | str | None) -> Path:
    gates: Path = log.gates_dir(slug, home)  # pyright: ignore[reportUnknownMemberType]
    return gates / STAMPS


def _read(path: Path) -> dict[str, int]:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, ValueError):
        return {}


def _write(path: Path, stamps: dict[str, int]) -> None:
    spare = path.with_suffix(".tmp")
    spare.write_text(json.dumps(stamps))
    os.replace(spare, path)


def _note(what: str, failure: Failure, exc: BaseException) -> None:
    print(f"agentihooks gate {failure.gate}: {what}: {type(exc).__name__}: {exc}", file=sys.stderr)
