import os
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from scripts import agent_choice
from scripts.events.envelope import Envelope
from scripts.profiles import plugins
from scripts.swarm import capacity, quota_handoff
from scripts.swarm.store import MASTER, SwarmConfig
from scripts.swarm.templates import DEFAULT_PROFILES


def _launch(value: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value[key] for key in ("harness", "account", "profile") if key in value}


def restrictions(task: Mapping[str, Any]) -> dict[str, Any]:
    result = {key: task[key] for key in ("requires_harness", "profile", "avoid_harness") if key in task}
    if saved := task.get("launch_assignment"):
        result["launch_assignment"] = _launch(saved)
    if handoff := task.get("handoff_envelope"):
        result["handoff_envelope"] = {
            **({"reason": handoff["reason"]} if "reason" in handoff else {}),
            "launch": _launch(handoff.get("launch") or {}),
        }
    return result


def _saved(task: Mapping[str, Any]) -> dict[str, Any]:
    handoff = dict[str, Any](task.get("handoff_envelope") or {})
    return dict[str, Any](task.get("launch_assignment") or handoff.get("launch") or {})


def accounts(environ: dict[str, str], now: float, refresh: bool = False, redis: Any = None) -> list[capacity.Account]:
    """The account rows an event offer seat check reads: the hive quota reports on ``redis`` for the hub, the hive's
    own readings for a spoke."""
    read: Callable[..., list[capacity.Account]] = getattr(capacity, "accounts")
    return read(environ, now, refresh=refresh, redis=redis)


def permitted(config: SwarmConfig, lane: str, task: Mapping[str, Any]) -> tuple[str, ...]:
    lanes = dict[str, Any](getattr(config, "lanes", {}))
    chosen = dict[str, Any](lanes.get(lane, {}))
    requested = chosen.get("agent") or ""
    requested = "" if requested == "auto" else requested
    required = task.get("requires_harness")
    if requested and required and requested != required:
        return ()
    wanted = required or requested
    saved = _saved(task)
    profile = saved.get("profile") or task.get("profile") or chosen.get("profile") or DEFAULT_PROFILES[lane]
    if plugins.claude_only(profile):
        return ("claude",) if wanted in ("", "claude") else ()
    return (wanted,) if wanted in agent_choice.AGENTS else () if wanted else agent_choice.AGENTS


def available(
    config: SwarmConfig,
    lane: str,
    task: Mapping[str, Any],
    rows: Sequence[capacity.Account],
    environ: Mapping[str, str],
) -> tuple[str, ...]:
    thresholds = getattr(quota_handoff.Thresholds, "from_env")(dict(environ))
    saved = _saved(task)
    handoff = dict[str, Any](task.get("handoff_envelope") or {})
    predecessor = (saved.get("harness"), saved.get("account")) if handoff.get("reason") == "quota" else None
    return tuple(
        harness
        for harness in permitted(config, lane, task)
        if any(
            row.harness == harness and not getattr(quota_handoff, "exclusion")(row, thresholds, predecessor)
            for row in rows
        )
    )


class Fit:
    def __init__(
        self,
        config: SwarmConfig,
        lane: str,
        fit: Callable[[Envelope], bool],
        read: Callable[..., list[capacity.Account]] | None = None,
    ) -> None:
        self.config, self.lane, self.fit = config, lane, fit
        self.read: Callable[..., list[capacity.Account]] = read or accounts
        self.environ = dict(os.environ)
        self.rows: list[capacity.Account] | None = None
        self.held = False

    def __call__(self, event: Envelope) -> bool:
        if not self.fit(event):
            return False
        if self.lane == MASTER:
            return True
        data = dict[str, Any](getattr(event, "data"))
        descriptor = data.get("quota")
        if not isinstance(descriptor, dict):
            self.held = True
            return False
        if not permitted(self.config, self.lane, data["quota"]):
            return True
        if self.rows is None:
            self.rows = self.read(self.environ, time.time(), refresh=False)
        admitted = bool(available(self.config, self.lane, data["quota"], self.rows, self.environ))
        self.held = self.held or not admitted
        return admitted
