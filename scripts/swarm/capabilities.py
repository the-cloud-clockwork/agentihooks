"""What one execution's transport can do, read from the harness and transport recorded at launch."""

from dataclasses import dataclass

from scripts.swarm.store import APP_SERVER_TRANSPORT, AgentRecord


@dataclass(frozen=True)
class Capabilities:
    push_inbox: bool
    native_terminal_attach: bool
    requires_foreground_inbox_wait: bool


CLAUDE = Capabilities(push_inbox=True, native_terminal_attach=False, requires_foreground_inbox_wait=False)
CODEX_PUSH = Capabilities(push_inbox=True, native_terminal_attach=True, requires_foreground_inbox_wait=False)
CODEX_LEGACY = Capabilities(push_inbox=False, native_terminal_attach=False, requires_foreground_inbox_wait=True)
UNKNOWN = Capabilities(push_inbox=False, native_terminal_attach=False, requires_foreground_inbox_wait=False)


def of(harness: str | None, transport: str | None) -> Capabilities:
    if harness == "claude":
        return CLAUDE
    if harness == "codex":
        return CODEX_PUSH if transport == APP_SERVER_TRANSPORT else CODEX_LEGACY
    return UNKNOWN


def of_agent(agent: AgentRecord) -> Capabilities:
    return of(agent.harness, agent.transport)
