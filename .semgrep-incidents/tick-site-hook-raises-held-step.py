from dataclasses import dataclass, field
from typing import Any, Protocol

from scripts.tick.steps import host as host_steps
from scripts.tick.steps.observe import raise_held


@dataclass
class _Wrapper:
    box: dict[str, Any] = field(default_factory=dict[str, Any])
    judging: dict[str, Any] = field(default_factory=dict[str, Any])

    def _judged(self) -> bool:
        # ruleid: tick-site-hook-raises-held-step
        host_steps.raise_held(self.judging)
        return True

    def shut(self, step: Any) -> bool:
        # ruleid: tick-site-hook-raises-held-step
        raise_held(self.box)
        return False


@dataclass
class TickSites:
    box: dict[str, Any] = field(default_factory=dict[str, Any])

    def _listed(self) -> bool:
        # ruleid: tick-site-hook-raises-held-step
        host_steps.raise_held(self.box)
        return True


@dataclass
class _RelaySites:
    box: dict[str, Any] = field(default_factory=dict[str, Any])

    def _relayed(self) -> bool:
        # ruleid: tick-site-hook-raises-held-step
        host_steps.raise_held(self.box)
        return True


class _Hooks(Protocol):
    box: dict[str, Any]

    def _viewed(self) -> bool:
        # ruleid: tick-site-hook-raises-held-step
        raise_held(self.box)
        return True


def run_tick(box: dict[str, Any], judging: dict[str, Any]) -> list[str]:
    try:
        # ok: tick-site-hook-raises-held-step
        host_steps.raise_held(box)
    finally:
        # ok: tick-site-hook-raises-held-step
        host_steps.raise_held(judging)
    return []
