"""The interface every gate implements, and the call, identity and decision it works on."""

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, Self, cast, runtime_checkable


@dataclass(frozen=True)
class Call:
    tool: str
    tool_input: dict[str, Any] = field(default_factory=dict)
    cwd: str = ""
    session: str = ""

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> Self:
        return cls(
            tool=str(payload.get("tool_name") or ""),
            tool_input=payload.get("tool_input") or {},
            cwd=str(payload.get("cwd") or ""),
            session=str(payload.get("session_id") or ""),
        )

    @property
    def command(self) -> str:
        return str(self.tool_input.get("command") or "")


@dataclass(frozen=True)
class Who:
    name: str = ""
    swarm: str = ""
    lane: str = ""
    task: str = ""
    profile: str = ""

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> Self:
        import scripts.swarm_ledger.ledger_link
        from hooks.context.account_sessions import agent_pid
        from hooks.context.broadcast import session_name, session_swarm

        env = os.environ if environ is None else environ
        pid = agent_pid() if env is os.environ else 0
        name = session_name(pid) if pid else ""
        swarm = env.get("AGENTIHOOKS_SWARM", "") or (
            session_swarm(pid)
            if pid and cast(Callable[[Mapping[str, str]], bool], scripts.swarm_ledger.ledger_link.remote)(env)
            else ""
        )
        return cls(
            name=name or env.get("AGENTIHOOKS_AGENT_NAME", ""),
            swarm=swarm,
            lane=env.get("AGENTIHOOKS_SWARM_LANE", ""),
            task=env.get("AGENTIHOOKS_SWARM_TASK", ""),
            profile=env.get("AGENTIHOOKS_PROFILE", ""),
        )

    @property
    def pinned(self) -> bool:
        return bool(self.swarm and self.name)


@dataclass(frozen=True)
class Decision:
    allowed: bool = True
    reason: str = ""

    @classmethod
    def deny(cls, reason: str) -> Self:
        return cls(allowed=False, reason=reason)


@runtime_checkable
class Gate(Protocol):
    name: str
    default_mode: str

    def matches(self, call: Call) -> bool: ...

    def decide(self, call: Call, who: Who, state: object) -> Decision: ...
