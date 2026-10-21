"""`python -m scripts.codex_session attach`: a native Codex terminal viewing an execution's recorded thread."""

import argparse
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from scripts.codex_session.binding import CONNECTED, EXITED, FAILED, BindingStore, TransportRecord

if TYPE_CHECKING:
    from scripts.herdr_host import Placement
    from scripts.swarm.store import AgentRecord, RedisStore

READY_TIMEOUT_S = 120.0
POLL_S = 0.5
ATTEMPTS = 3
EARLY_EXIT_S = 5.0
ENDED = "the execution's app-server {}; there is no thread to attach"
NOT_READY = "the execution has no connected thread after {:g} seconds"
NO_ATTACH = "{} runs no app-server a native terminal can attach to"
NO_THREAD = "{} has no connected thread to reopen"
LOCAL = "local"
LAUNCHING_HARNESSES = ("codex", "")
Read = Callable[[], TransportRecord | None]
Run = Callable[[list[str]], int]
Open = Callable[[str, list[str]], "Placement"]


class AttachError(RuntimeError):
    pass


def viewer_argv(codex_bin: str, record: TransportRecord) -> list[str]:
    return [
        codex_bin,
        "-c",
        "check_for_update_on_startup=false",
        "resume",
        "--remote",
        record.endpoint_ref,
        record.thread_id,
    ]


def attachable(record: TransportRecord | None) -> bool:
    return record is not None and record.state == CONNECTED and bool(record.thread_id)


def wait_attachable(
    read: Read,
    timeout: float,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> TransportRecord:
    deadline = clock() + timeout
    while True:
        record = read()
        if attachable(record):
            return record
        if record is not None and record.state in (FAILED, EXITED):
            raise AttachError(ENDED.format(record.state))
        if clock() >= deadline:
            raise AttachError(NOT_READY.format(timeout))
        sleep(POLL_S)


def attach(
    read: Read,
    run: Run,
    codex_bin: str,
    timeout: float,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """A viewer that exits at once is retried: the thread accepts resume only after its first turn wrote a rollout."""
    for _ in range(ATTEMPTS):
        record = wait_attachable(read, timeout, clock, sleep)
        started = clock()
        code = run(viewer_argv(codex_bin, record))
        if code == 0 or clock() - started >= EARLY_EXIT_S:
            return code
        sleep(POLL_S)
    return code


def recorded(slug: str, name: str) -> Read:
    from scripts.swarm import capabilities, live_binding
    from scripts.swarm.store import connect

    store = connect()

    def read() -> TransportRecord | None:
        agent = next((agent for agent in store.agents(slug) if agent.name == name), None)
        if agent is None or (not agent.execution_id and agent.harness in LAUNCHING_HARNESSES):
            return None
        if not capabilities.of_agent(agent).native_terminal_attach:
            raise AttachError(NO_ATTACH.format(name))
        return live_binding.transport(store, agent)

    return read


def reopenable(store: "RedisStore", slug: str, execution_id: str) -> "AgentRecord | None":
    from scripts.swarm import capabilities

    agent = next((agent for agent in store.agents(slug) if agent.execution_id == execution_id), None)
    if agent is None or agent.runtime_backend != LOCAL or not capabilities.of_agent(agent).native_terminal_attach:
        return None
    return agent


def recorded_terminals(name: str, viewers: dict[str, str], environ: dict[str, str]) -> dict[str, str]:
    """herdr reuses a closed pane's id, so a pane is the agent's only on the terminal recorded at its launch or as its
    viewer."""
    from scripts import herdr_panes
    from scripts.swarm import naming

    launched = {
        r.pane_id: r.terminal_id
        for r in herdr_panes.load(environ)
        if r.kind == "init-agent" and naming.plain(r.owner_session) == naming.plain(name)
    }
    return {pane: terminal for pane, terminal in {**launched, **viewers}.items() if terminal}


def reopen(
    store: "RedisStore",
    slug: str,
    agent: "AgentRecord",
    open_viewer: Open,
    codex_bin: str,
    close_earlier: Callable[[str], None] = lambda pane: None,
) -> dict:
    from scripts.swarm import live_binding

    record = live_binding.transport(store, agent)
    if not attachable(record):
        raise AttachError(NO_THREAD.format(agent.name))
    placed = open_viewer(agent.name, viewer_argv(codex_bin, record))
    store.execution_registry.set_pane(slug, agent.execution_id, placed.pane_id)
    if agent.pane_id and agent.pane_id != placed.pane_id:
        close_earlier(agent.pane_id)
    return {"agent": agent.name, "thread": record.thread_id, "pane": placed.pane_id}


def local_terminal(store: "RedisStore", slug: str, execution_id: str, environ: dict[str, str]) -> dict | None:
    from scripts import herdr_host
    from scripts.swarm import naming

    agent = reopenable(store, slug, execution_id)
    if agent is None:
        return None
    config = store.config(slug)
    workspace = naming.space(config.repo, config.code, config.slug)
    bindings = BindingStore(store)

    def open_viewer(title: str, argv: list[str]) -> "Placement":
        placed = herdr_host.open_command(Path(config.repo), title, argv, workspace, environ)
        bindings.add_viewer(slug, execution_id, placed.pane_id, placed.terminal_id)
        return placed

    def close_earlier(pane_id: str) -> None:
        terminal = recorded_terminals(agent.name, bindings.viewers(slug, execution_id), environ).get(pane_id)
        listed = {p["pane_id"]: p.get("terminal_id") for p in herdr_host._cli(["pane", "list"], environ)["panes"]}
        if terminal and listed.get(pane_id) == terminal:
            herdr_host._cli(["pane", "close", pane_id], environ)
            bindings.forget_viewers(slug, [pane_id])

    return reopen(store, slug, agent, open_viewer, "codex", close_earlier)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m scripts.codex_session attach")
    parser.add_argument("--slug", required=True)
    parser.add_argument("--agent", required=True)
    parser.add_argument("--codex-bin", default="codex")
    parser.add_argument("--timeout", type=float, default=READY_TIMEOUT_S)
    return parser


def main(argv: list[str], read: Read | None = None, run: Run | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return attach(
            read or recorded(args.slug, args.agent),
            run or (lambda command: subprocess.run(command).returncode),
            args.codex_bin,
            args.timeout,
        )
    except AttachError as exc:
        print(f"agentihooks codex-session: {exc}", file=sys.stderr)
        return 3
