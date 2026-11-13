"""Hourly operating system safety nudge for every live swarm master on this host.

Masters record themselves in a registry file under the agentihooks home when they join their ledger as orchestrator
and on later tool calls. An hourly operating system cron entry runs one round: for each registered master whose
process still lives, it types the safety nudge into the master's herdr pane when that pane sits idle at its prompt.
The same round runs the quick tick over every quick swarm on the host, which has no tick of its own.
Nothing here reads Redis, the swarm tick or the Anton controller.
"""

import argparse
import contextlib
import fcntl
import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Generator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

TEXT = "Safety Nudge: In case the main tick or controller is down"
REGISTRY = "master-registry.json"
LOG = "safety-nudge.log"
MARKER = "# agentihooks-safety-nudge"
SCHEDULE = "0 * * * *"
CRON = "crontab"
PROC = Path("/proc")
BASE_PATH = ("/usr/local/bin", "/usr/bin", "/bin")
Entry = dict[str, Any]


@dataclass(frozen=True)
class Pane:
    """The fields of a registered master that the herdr messenger reads."""

    name: str
    harness: str
    pane_id: str


class Messenger(Protocol):
    """The herdr pane reads and prompt the inbox wake uses."""

    def agent_status(self, agent: Pane) -> str:
        """The agent status herdr reports for the pane."""
        ...

    def typed_input(self, agent: Pane) -> str:
        """Text waiting on the pane's input line."""
        ...

    def prompt(self, agent: Pane, text: str) -> None:
        """Submit ``text`` as a prompt in the pane."""
        ...


def home(environ: Mapping[str, str]) -> Path:
    """The agentihooks home folder."""
    return Path(environ.get("AGENTIHOOKS_HOME") or Path.home() / ".agentihooks")


def registry_path(environ: Mapping[str, str]) -> Path:
    """The registry file of live masters on this host."""
    return home(environ) / REGISTRY


def log_path(environ: Mapping[str, str]) -> Path:
    """The file every nudge round appends its sends to."""
    return home(environ) / "logs" / LOG


def alive(pid: int, proc: Path = PROC) -> bool:
    """True while process ``pid`` exists on this host."""
    return pid > 0 and (proc / str(pid)).exists()


def load(path: Path) -> dict[str, Entry]:
    """The registry entries keyed by master name; an unreadable file reads as empty."""
    try:
        data: Any = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    try:
        items: Any = data.items()
    except AttributeError:
        return {}
    return {str(name): entry for name, entry in items if isinstance(entry, dict)}


def save(path: Path, entries: dict[str, Entry]) -> None:
    """Write the registry atomically through a temporary file in the same folder."""
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(entries))
    os.replace(temporary, path)


@contextlib.contextmanager
def locked(path: Path) -> Generator[None, None, None]:
    """Hold an exclusive lock on the registry while it is read, changed and written; closing releases it."""
    path.parent.mkdir(exist_ok=True)
    with path.with_name(f".{path.name}.lock").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def live(entries: dict[str, Entry], proc: Path = PROC) -> dict[str, Entry]:
    """The entries whose process is still running."""
    return {name: entry for name, entry in entries.items() if alive(entry.get("pid", 0), proc)}


def entry_of(environ: Mapping[str, str], pid: int, now: float) -> Entry | None:
    """The registry entry for this session, None unless it is a swarm master inside a herdr pane."""
    from scripts.swarm.naming import lane_of

    name, slug, pane = (environ.get(k) for k in ("AGENTIHOOKS_AGENT_NAME", "AGENTIHOOKS_SWARM", "HERDR_PANE_ID"))
    if not (name and slug and pane) or lane_of(name) != "master":
        return None
    harness = environ.get("AGENTIHOOKS_TARGET") or "claude"
    return {"seat": name, "slug": slug, "harness": harness, "pane": pane, "pid": pid, "last_seen": int(now)}


def record(
    environ: Mapping[str, str], pid: int | None = None, now: float | None = None, proc: Path = PROC
) -> Entry | None:
    """Record or refresh this master in the registry and drop entries whose process is gone."""
    if pid is None:
        from hooks.context.account_sessions import agent_pid

        pid = agent_pid()
    entry = entry_of(environ, pid, time.time() if now is None else now)
    if entry is None:
        return None
    path = registry_path(environ)
    with locked(path):
        entries = live(load(path), proc)
        entries[entry["seat"]] = entry
        save(path, entries)
    return entry


def messenger() -> Messenger:
    """The herdr messenger the inbox wake uses to read pane state and type prompts."""
    from scripts.swarm.delivery import HerdrMessenger

    return HerdrMessenger()


def nudge(herdr: Messenger, entry: Entry) -> str:
    """Type the nudge into an idle master pane; the outcome is sent, failed or the reason it was skipped."""
    from scripts.swarm.delivery import READY

    pane = Pane(entry["seat"], entry["harness"], entry["pane"])
    try:
        status = herdr.agent_status(pane)
        if status not in READY:
            return f"skipped-{status}"
        if herdr.typed_input(pane):
            return "skipped-typed"
        herdr.prompt(pane, TEXT)
    except Exception as error:
        return f"failed-{type(error).__name__}"
    return "sent"


def _log(environ: Mapping[str, str], rows: list[Entry]) -> None:
    target = log_path(environ)
    target.parent.mkdir(exist_ok=True)
    with target.open("a") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")


def run_round(
    environ: Mapping[str, str], herdr: Messenger | None = None, now: float | None = None, proc: Path = PROC
) -> list[Entry]:
    """One nudge round over the live masters in the registry, then the quick tick, each outcome appended to the log."""
    from scripts.quick.tick import run_round as quick_round

    path = registry_path(environ)
    with locked(path):
        entries = live(load(path), proc)
        save(path, entries)
    at = int(time.time() if now is None else now)
    sender = (herdr or messenger()) if entries else None
    results = [{**entry, "at": at, "outcome": nudge(sender, entry)} for entry in entries.values() if sender]
    _log(environ, results or [{"at": at, "outcome": "no-masters"}])
    quick = quick_round(environ)
    _log(environ, quick)
    return results + quick


def command(environ: Mapping[str, str]) -> str:
    """The cron line: PATH reaches agentihooks and herdr, and the round's output goes to the log."""
    exe = shutil.which("agentihooks", path=environ.get("PATH")) or "agentihooks"
    found = [shutil.which(tool, path=environ.get("PATH")) for tool in ("agentihooks", "herdr")]
    dirs = dict.fromkeys([*(str(Path(tool).parent) for tool in found if tool), *BASE_PATH])
    socket = environ.get("HERDR_SOCKET_PATH")
    prefix = f"HERDR_SOCKET_PATH={socket} " if socket else ""
    return f"{SCHEDULE} PATH={':'.join(dirs)} {prefix}{exe} safety-nudge round >/dev/null 2>&1 {MARKER}"


def table(runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run) -> list[str]:
    """The current user's cron lines; none when the user has no table yet."""
    done = runner([CRON, "-l"], capture_output=True, text=True)
    return done.stdout.splitlines() if done.returncode == 0 else []


def install(
    environ: Mapping[str, str], runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run
) -> str:
    """Register the hourly round in the user's cron table, replacing an earlier entry of this job."""
    line = command(environ)
    lines = [kept for kept in table(runner) if MARKER not in kept] + [line]
    done = runner([CRON, "-"], input="\n".join(lines) + "\n", capture_output=True, text=True)
    if done.returncode != 0:
        raise RuntimeError(f"{CRON} refused the entry: {done.stderr.strip()}")
    return line


def check(
    environ: Mapping[str, str],
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    proc: Path = PROC,
) -> Entry:
    """Whether the hourly job exists, and the registered pane of this master or of every live master."""
    job = next((line for line in table(runner) if MARKER in line), "")
    entries = live(load(registry_path(environ)), proc)
    name = environ.get("AGENTIHOOKS_AGENT_NAME")
    mine = {name: entries[name]} if name in entries else entries
    return {"installed": bool(job), "job": job, "masters": list(mine.values())}


def main(argv: list[str] | None = None) -> int:
    """agentihooks safety-nudge round|install|check."""
    parser = argparse.ArgumentParser(prog="agentihooks safety-nudge")
    parser.add_argument("action", choices=["round", "install", "check", "record"])
    args = parser.parse_args(argv)
    environ = dict(os.environ)
    if args.action == "round":
        print(json.dumps(run_round(environ)))
        return 0
    if args.action == "install":
        print(json.dumps({"installed": install(environ)}))
        return 0
    if args.action == "record":
        print(json.dumps(record(environ)))
        return 0
    report = check(environ)
    print(json.dumps(report))
    return 0 if report["installed"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
