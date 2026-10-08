"""End what a swarm agent leaves running, chosen by the process id recorded at spawn and the task's scratch homes,
never by the agent's bare name, which subagents, resumed duplicates and proof daemons can share."""

import os
import signal
import time
from dataclasses import dataclass
from pathlib import Path

from hooks.proc import Process, processes
from scripts.swarm import naming

SCRATCH = Path.home() / "scratchpad"
NAME_KEY = "AGENTIHOOKS_AGENT_NAME"
HOME_KEYS = ("HOME", "CODEX_HOME", "CLAUDE_CONFIG_DIR")
POLL_S, TRIES = 0.05, 60


@dataclass(frozen=True)
class Outcome:
    ended: tuple[int, ...] = ()
    process: int = 0
    refusal: str = ""


@dataclass(frozen=True)
class Targets:
    groups: frozenset[int] = frozenset()
    singles: frozenset[int] = frozenset()


def scratch_homes(slug: str, task: str, root: Path | None = None) -> list[Path]:
    root = SCRATCH if root is None else root
    return sorted(path.resolve() for path in root.glob(f"*/{naming.scratch_folder(slug, task)}") if path.is_dir())


def _environ(pid: int) -> dict[str, str]:
    from scripts.terminate_agent import agent_environ

    keys = (NAME_KEY, *HOME_KEYS)
    return dict(zip(keys, agent_environ(pid, keys)))


def carries(process: Process, name: str) -> bool:
    named = {process.argv[i + 1] for i, word in enumerate(process.argv[:-1]) if word == "--name"}
    named.add(_environ(process.pid)[NAME_KEY])
    return name in {naming.resolve_name(found) for found in named if found}


def _from(process: Process, homes: list[Path]) -> bool:
    env = _environ(process.pid)
    places = [env[key] for key in HOME_KEYS]
    try:
        places.append(os.readlink(f"/proc/{process.pid}/cwd"))
    except OSError:
        pass
    return any(place and Path(place).is_relative_to(home) for place in places for home in homes)


def _caller(table: dict[int, Process]) -> set[int]:
    chain, pid = set(), os.getpid()
    while pid in table and pid not in chain:
        chain.add(pid)
        pid = table[pid].ppid
    return chain


def _split(found: list[Process], launch: Process | None, table: dict[int, Process]) -> Targets:
    """The launch process and each group leader found end with their whole group, any other process alone; a group
    holding the caller is never signalled whole and the caller itself never at all."""
    caller = _caller(table)
    unsafe = {table[pid].pgid for pid in caller} | {0, 1}
    found = [*found, *([launch] if launch is not None else [])]
    whole = {p.pgid for p in found if p.pid == p.pgid or p is launch} - unsafe
    singles = {p.pid for p in found if p.pgid not in whole} - caller
    return Targets(frozenset(whole), frozenset(singles))


def targets(name: str, pid: int | str | None, homes: list[Path], start: int = 0) -> Targets:
    table = processes()
    launch = table.get(int(pid)) if pid else None
    if launch is not None and (not carries(launch, name) or start and launch.start_time != start):
        launch = None
    loose = [p for p in table.values() if homes and _from(p, homes)]
    return _split(loose, launch, table)


def _members(found: Targets) -> dict[int, int]:
    return {p.pid: p.start_time for p in processes().values() if p.pgid in found.groups or p.pid in found.singles}


def _alive(members: dict[int, int]) -> list[int]:
    table = processes()
    return sorted(
        pid
        for pid, started in members.items()
        if pid in table and table[pid].start_time == started and table[pid].state != "Z"
    )


def _wait(members: dict[int, int]) -> list[int]:
    for _ in range(TRIES):
        left = _alive(members)
        if not left:
            return []
        time.sleep(POLL_S)
    return _alive(members)


def _signal(found: Targets, sig: int) -> str:
    for send, ids in ((os.killpg, found.groups), (os.kill, found.singles)):
        for target in sorted(ids):
            try:
                send(target, sig)
            except ProcessLookupError:
                continue
            except OSError as exc:
                return f"signal to {target} refused: {exc.strerror}"
    return ""


def end(found: Targets) -> Outcome:
    members, refused = _members(found), ""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        refused = _signal(found, sig) or refused
        left = _wait(members)
        if not left:
            return Outcome(tuple(sorted(members)))
    survivors = ", ".join(map(str, left))
    return Outcome(tuple(sorted(set(members) - set(left))), left[0], refused or f"survived SIGKILL: {survivors}")


def retire(name: str, pid: int | None, homes: list[Path], start: int = 0) -> Outcome:
    return end(targets(name, pid, homes, start))


def reap(pids: list[int]) -> Outcome:
    """End agent processes holding a name the swarm no longer records, each with its group when it leads one."""
    table = processes()
    return end(_split([table[pid] for pid in pids if pid in table], None, table))
