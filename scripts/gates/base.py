"""The interface every gate implements, and the call, identity and decision it works on."""

import os
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class Call:
    tool: str
    tool_input: dict = field(default_factory=dict)
    cwd: str = ""

    @classmethod
    def from_payload(cls, payload):
        return cls(
            tool=str(payload.get("tool_name") or ""),
            tool_input=payload.get("tool_input") or {},
            cwd=str(payload.get("cwd") or ""),
        )

    @property
    def command(self):
        return str(self.tool_input.get("command") or "")


@dataclass(frozen=True)
class Who:
    name: str = ""
    swarm: str = ""
    lane: str = ""
    task: str = ""
    profile: str = ""

    @classmethod
    def from_env(cls, environ=None):
        from hooks.context.account_sessions import agent_pid
        from hooks.context.broadcast import session_name

        env = os.environ if environ is None else environ
        name = session_name(agent_pid()) if env is os.environ else ""
        return cls(
            name=name or env.get("AGENTIHOOKS_AGENT_NAME", ""),
            swarm=env.get("AGENTIHOOKS_SWARM", ""),
            lane=env.get("AGENTIHOOKS_SWARM_LANE", ""),
            task=env.get("AGENTIHOOKS_SWARM_TASK", ""),
            profile=env.get("AGENTIHOOKS_PROFILE", ""),
        )

    @property
    def pinned(self):
        return bool(self.swarm and self.name)


@dataclass(frozen=True)
class Decision:
    allowed: bool = True
    reason: str = ""

    @classmethod
    def deny(cls, reason):
        return cls(allowed=False, reason=reason)


@runtime_checkable
class Gate(Protocol):
    name: str
    default_mode: str

    def matches(self, call: Call) -> bool: ...

    def decide(self, call: Call, who: Who, state: object) -> Decision: ...
