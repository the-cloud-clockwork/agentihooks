"""How many more agents this host takes, from the one minute load per CPU, available memory and live agent sessions."""

import os
from dataclasses import dataclass
from pathlib import Path

from hooks.context import account_sessions

MEMORY_PER_AGENT_MB = 700
LOAD_HIGH = 1.5
LOAD_LOW = 1.0


@dataclass(frozen=True)
class Thresholds:
    load_high: float = LOAD_HIGH
    load_low: float = LOAD_LOW
    memory_per_agent_mb: int = MEMORY_PER_AGENT_MB


@dataclass(frozen=True)
class HostSample:
    load1: float
    cpus: int
    available_mb: int
    agents: int


@dataclass(frozen=True)
class Room:
    room: int
    reason: str


def memory_room(sample: HostSample, thresholds: Thresholds) -> int:
    return max(0, sample.available_mb // thresholds.memory_per_agent_mb)


def load_room(sample: HostSample, thresholds: Thresholds) -> int | None:
    if sample.agents <= 0 or sample.load1 <= 0:
        return None
    per_agent = sample.load1 / sample.agents
    return max(0, int((thresholds.load_high * sample.cpus - sample.load1) // per_agent))


def room(sample: HostSample, thresholds: Thresholds = Thresholds(), previous: int | None = None) -> Room:
    per_cpu = sample.load1 / max(1, sample.cpus)
    load = f"one minute load {per_cpu:.2f} per CPU"
    memory = memory_room(sample, thresholds)
    memory_text = f"{sample.available_mb} MB available memory fits {memory} at {thresholds.memory_per_agent_mb} MB each"
    if per_cpu > thresholds.load_high:
        return Room(0, f"{load} is above the high watermark {thresholds.load_high:.2f}, no room")
    if per_cpu >= thresholds.load_low:
        held = previous or 0
        if memory < held:
            return Room(memory, f"{load} is between the watermarks; {memory_text}, below the previous room of {held}")
        return Room(held, f"{load} is between the watermarks, the previous room of {held} holds")
    projected = load_room(sample, thresholds)
    if projected is not None and projected < memory:
        return Room(
            projected,
            f"{load} is below the low watermark; {sample.agents} live agents project load to the high watermark "
            f"after {projected} more",
        )
    return Room(memory, f"{load} is below the low watermark; {memory_text}")


def _mem_available_mb(proc: Path) -> int:
    for line in (proc / "meminfo").read_text().splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) // 1024
    return 0


def read_host(proc: Path = Path("/proc")) -> HostSample:
    agents = len(account_sessions.live_sessions(proc)) + account_sessions.live_codex_sessions(proc)
    return HostSample(
        load1=float((proc / "loadavg").read_text().split()[0]),
        cpus=os.cpu_count() or 1,
        available_mb=_mem_available_mb(proc),
        agents=agents,
    )
