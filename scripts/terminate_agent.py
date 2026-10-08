import argparse
import json
import os
import signal
import sys
import time
from dataclasses import dataclass, replace
from pathlib import Path

from hooks.proc import Process, _process, _target, processes
from hooks.targets import is_codex_memory_thread


@dataclass(frozen=True)
class Session:
    session_id: str
    target: str
    name: str
    process: Process
    cwd: str
    status: str


def _argument(argv: tuple[str, ...], option: str) -> str:
    try:
        return argv[argv.index(option) + 1]
    except (ValueError, IndexError):
        return ""


def _registry() -> dict[str, dict]:
    try:
        from hooks.context.broadcast import _load_sessions

        return _load_sessions()
    except Exception:
        return {}


def _name(process: Process, proc: Path, table: dict[int, Process]) -> str:
    named = _argument(process.argv, "--name")
    parent = table.get(process.ppid)
    if named or (parent and _target(parent)):
        return named
    return agent_environ(process.pid, ("AGENTIHOOKS_AGENT_NAME",), proc)[0]


def _main_thread(target: str, session_id: str, info: dict, records: dict[str, dict]) -> tuple[str, str]:
    cwd = str(info.get("cwd", ""))
    if target != "codex" or not cwd or not is_codex_memory_thread(cwd):
        return session_id, cwd
    threads = [
        (str(other.get("started_at", "")), other_id, str(other["cwd"]))
        for other_id, other in records.items()
        if other.get("pid") == info.get("pid")
        and other.get("status") in {"alive", "handed_off", "superseded"}
        and other.get("cwd")
        and not is_codex_memory_thread(str(other["cwd"]))
    ]
    if not threads:
        return session_id, cwd
    _, main_id, main_cwd = max(threads)
    return main_id, main_cwd


def sessions(proc: Path = Path("/proc"), registry: dict[str, dict] | None = None) -> list[Session]:
    table = processes(proc)
    records = _registry() if registry is None else registry
    result = []
    registered_pids = set()
    for session_id, info in records.items():
        if info.get("status") not in {"alive", "handed_off"}:
            continue
        try:
            pid = int(info.get("pid", 0))
        except (TypeError, ValueError):
            continue
        process = table.get(pid)
        target = _target(process) if process else ""
        if not process or not target:
            continue
        registered_pids.add(pid)
        session_id, cwd = _main_thread(target, session_id, info, records)
        result.append(
            Session(
                session_id=session_id,
                target=target,
                name=str(info.get("name") or "") or _name(process, proc, table),
                process=process,
                cwd=cwd,
                status=str(info.get("status", "alive")),
            )
        )
    for process in table.values():
        target = _target(process)
        if not target or process.pid in registered_pids:
            continue
        name = _name(process, proc, table)
        if target == "claude" and any(value in {"-p", "--print"} for value in process.argv[1:]):
            continue
        result.append(Session("", target, name, process, "", "unregistered"))
    from scripts.swarm.naming import resolve_name

    result = [replace(item, name=resolve_name(item.name)) for item in result]
    return sorted(result, key=lambda item: (item.target, item.name, item.session_id, item.process.pid))


def _label(session: Session) -> str:
    return session.name or session.session_id or str(session.process.pid)


def _print_sessions(items: list[Session]) -> None:
    print("TYPE\tNAME\tSESSION_ID\tPID\tPGID\tSTATUS\tCWD")
    for item in items:
        print(
            f"{item.target}\t{item.name or '-'}\t{item.session_id or '-'}\t{item.process.pid}\t"
            f"{item.process.pgid}\t{item.status}\t{item.cwd or '-'}"
        )


def resolve(items: list[Session], selector: str, target: str, names=None) -> Session:
    from scripts.swarm.naming import resolve_name

    canonical = names.resolve if names is not None else resolve_name
    selector = canonical(selector)
    scoped = [item for item in items if target == "any" or item.target == target]
    matches = [item for item in scoped if selector in {canonical(item.name), item.session_id, str(item.process.pid)}]
    unique = {(item.process.pid, item.session_id): item for item in matches}
    matches = list(unique.values())
    if not matches:
        raise ValueError(f"no live {target} session exactly matches {selector!r}")
    process_ids = {item.process.pid for item in matches}
    if len(matches) > 1 and len(process_ids) > 1:
        choices = ", ".join(item.session_id or str(item.process.pid) for item in matches)
        raise ValueError(f"ambiguous session name {selector!r}; select one UUID or PID: {choices}")
    return matches[0]


def _ancestors(pid: int, table: dict[int, Process]) -> set[int]:
    result = set()
    while pid > 1 and pid not in result:
        result.add(pid)
        process = table.get(pid)
        if process is None:
            break
        pid = process.ppid
    return result


def validate(
    session: Session, items: list[Session], proc: Path = Path("/proc"), force_shared: bool = False
) -> list[Process]:
    table = processes(proc)
    current = table.get(session.process.pid)
    if current is None or (current.start_time, current.comm, current.argv) != (
        session.process.start_time,
        session.process.comm,
        session.process.argv,
    ):
        raise ValueError("target process disappeared or its PID was reused")
    if current.pgid <= 1:
        raise ValueError(f"unsafe process group {current.pgid}")
    caller_ancestors = _ancestors(os.getpid(), table)
    caller_groups = {table[pid].pgid for pid in caller_ancestors if pid in table}
    if current.pgid in caller_groups or any(pid in caller_ancestors for pid in (current.pid, current.pgid)):
        raise ValueError("target overlaps the current caller")
    members = [item for item in table.values() if item.pgid == current.pgid and item.state not in {"Z", "X"}]
    if not members:
        raise ValueError("target process group has no live members")
    if any(item.sid != current.sid for item in members):
        raise ValueError("target process group spans multiple sessions")
    shared = {
        item.session_id
        for item in items
        if item.process.pid == current.pid and item.session_id and item.session_id != session.session_id
    }
    if shared and not force_shared:
        ids = ", ".join(sorted(shared))
        raise ValueError(f"target process is shared by other session UUIDs: {ids}; use --force-shared")
    return members


def _alive(identities: dict[int, tuple[int, str]], proc: Path) -> list[Process]:
    result = []
    for pid, identity in identities.items():
        current = _process(pid, proc)
        if current and current.state not in {"Z", "X"} and (current.start_time, current.comm) == identity:
            result.append(current)
    return result


def terminate(session: Session, members: list[Process], timeout: float, proc: Path = Path("/proc")) -> bool:
    identities = {item.pid: (item.start_time, item.comm) for item in members}
    os.killpg(session.process.pgid, signal.SIGTERM)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and _alive(identities, proc):
        time.sleep(0.1)
    survivors = _alive(identities, proc)
    escalated = bool(survivors)
    if survivors:
        current = _process(session.process.pid, proc)
        if current and (current.start_time, current.comm, current.argv) != (
            session.process.start_time,
            session.process.comm,
            session.process.argv,
        ):
            raise RuntimeError("target identity changed before SIGKILL")
        os.killpg(session.process.pgid, signal.SIGKILL)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and _alive(identities, proc):
            time.sleep(0.1)
    remaining = _alive(identities, proc)
    if remaining:
        raise RuntimeError("target processes survived SIGKILL: " + ", ".join(str(item.pid) for item in remaining))
    return escalated


def agent_environ(pid: int, keys: tuple[str, ...], proc: Path = Path("/proc")) -> tuple[str, ...]:
    """Values of only the named keys from a process environment; empty when unreadable or unset."""
    try:
        raw = (proc / str(pid) / "environ").read_bytes()
    except OSError:
        return tuple("" for _ in keys)
    wanted = dict.fromkeys(keys, "")
    for item in raw.split(b"\0"):
        key, _, value = item.partition(b"=")
        if key.decode(errors="replace") in wanted:
            wanted[key.decode(errors="replace")] = value.decode(errors="replace")
    return tuple(wanted[key] for key in keys)


def herdr_pane(pid: int, proc: Path = Path("/proc")) -> tuple[str, str]:
    """(HERDR_PANE_ID, HERDR_SOCKET_PATH) of an agent running in a herdr pane; empty outside herdr."""
    pane, socket = agent_environ(pid, ("HERDR_PANE_ID", "HERDR_SOCKET_PATH"), proc)
    return pane, socket


def _close_pane(pane: str, socket: str) -> str:
    from scripts import herdr_host

    try:
        herdr_host._cli(
            ["pane", "close", pane],
            {**os.environ, "HERDR_SOCKET_PATH": herdr_host.server_socket({"HERDR_SOCKET_PATH": socket})},
        )
    except (herdr_host.HerdrError, OSError) as exc:
        return "closed" if "not found" in str(exc) else f"close-failed ({exc})"
    return "closed"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="List or terminate a Claude Code or Codex agent session")
    parser.add_argument("selector", nargs="?", default="", help="Exact session name, UUID, or PID")
    parser.add_argument("--type", choices=["claude", "codex", "any"], default="any")
    parser.add_argument("--list", action="store_true", help="List live sessions")
    parser.add_argument("--dry-run", action="store_true", help="Resolve and validate without sending signals")
    parser.add_argument(
        "--force-shared", action="store_true", help="Allow terminating a process shared by multiple UUIDs"
    )
    parser.add_argument("--term-timeout", type=float, default=3.0)
    parser.add_argument("--keep-pane", action="store_true", help="Leave the agent's herdr pane open")
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.term_timeout <= 0:
        print("terminate-agent: --term-timeout must be positive", file=sys.stderr)
        return 2
    items = sessions()
    scoped = [item for item in items if args.type == "any" or item.target == args.type]
    if args.list:
        _print_sessions(scoped)
        return 0
    if not args.selector:
        print("terminate-agent: selector is required unless --list is used", file=sys.stderr)
        return 2
    try:
        selected = resolve(items, args.selector, args.type)
        members = validate(selected, items, force_shared=args.force_shared)
    except ValueError as exc:
        print(f"terminate-agent: {exc}", file=sys.stderr)
        return 2
    report = {
        "action": "dry-run" if args.dry_run else "terminate",
        "type": selected.target,
        "name": selected.name,
        "session_id": selected.session_id,
        "pid": selected.process.pid,
        "pgid": selected.process.pgid,
        "members": [item.pid for item in members],
    }
    pane, socket = herdr_pane(selected.process.pid)
    if pane:
        report["pane_id"] = pane
    if args.json:
        print(json.dumps(report, sort_keys=True))
    else:
        print(" ".join(f"{key}={value}" for key, value in report.items()))
    if args.dry_run:
        print("result=validated signal=none")
        return 0
    try:
        escalated = terminate(selected, members, args.term_timeout)
    except (OSError, RuntimeError) as exc:
        print(f"terminate-agent: {exc}", file=sys.stderr)
        return 1
    print(f"result=terminated escalation={'SIGKILL' if escalated else 'none'}")
    if pane:
        print(f"pane_id={pane} pane={'kept' if args.keep_pane else _close_pane(pane, socket)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
