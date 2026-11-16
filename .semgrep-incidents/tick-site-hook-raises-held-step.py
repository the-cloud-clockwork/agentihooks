from dataclasses import dataclass, field
from typing import Any

from scripts.tick.steps import host as host_steps
from scripts.tick.steps.observe import raise_held


@dataclass
class _Wrapper:
    box: dict[str, Any] = field(default_factory=dict[str, Any])
    judging: dict[str, Any] = field(default_factory=dict[str, Any])
    checked: list[str] = field(default_factory=list[str])

    def _judged(self) -> bool:
        # ruleid: tick-site-hook-raises-held-step
        host_steps.raise_held(self.judging)
        return True

    def shut(self, step: Any) -> bool:
        # ruleid: tick-site-hook-raises-held-step
        raise_held(self.box)
        return False

    def _kept(self) -> bool:
        if "judged" in self.judging:
            # ok: tick-site-hook-raises-held-step
            self.checked = list(self.judging["judged"])
        return True


@dataclass
class TickSites:
    box: dict[str, Any] = field(default_factory=dict[str, Any])

    def _listed(self) -> bool:
        # ruleid: tick-site-hook-raises-held-step
        host_steps.raise_held(self.box)
        return True


def run_tick(box: dict[str, Any], judging: dict[str, Any]) -> list[str]:
    try:
        # ok: tick-site-hook-raises-held-step
        host_steps.raise_held(box)
    finally:
        # ok: tick-site-hook-raises-held-step
        host_steps.raise_held(judging)
    return []
