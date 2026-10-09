import math
import os
from dataclasses import dataclass
from pathlib import Path

from hooks.context import account_sessions

MEMORY_PER_AGENT_MB = 700
LOAD_HIGH = 1.5
LOAD_LOW = 1.0
LOAD, MEMORY, UNKNOWN = "load", "memory", "unknown"


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
    room: int | None
    reason: str
    limit: str = ""


def thresholds(config) -> Thresholds:
    return Thresholds(config.load_high, config.load_low, config.memory_per_agent_mb)


def spawn_room(config, sample: HostSample | None, previous: int | None) -> Room:
    if sample is None:
        return Room(None, "host unknown: the process files cannot be read, so spawns pass", UNKNOWN)
    return room(sample, thresholds(config), previous)


def memory_room(sample: HostSample, thresholds: Thresholds) -> int:
    return max(0, sample.available_mb // thresholds.memory_per_agent_mb)


def load_room(sample: HostSample, thresholds: Thresholds) -> int | None:
    if sample.agents <= 0 or sample.load1 <= 0:
        return None
    per_agent = sample.load1 / sample.agents
    return max(0, math.floor((thresholds.load_high * sample.cpus - sample.load1) / per_agent))


def room(sample: HostSample, thresholds: Thresholds = Thresholds(), previous: int | None = None) -> Room:
    per_cpu = sample.load1 / max(1, sample.cpus)
    load = f"one minute load {per_cpu:.2f} per CPU"
    memory = memory_room(sample, thresholds)
    memory_text = f"{sample.available_mb} MB available memory fits {memory} at {thresholds.memory_per_agent_mb} MB each"
    if per_cpu > thresholds.load_high:
        return Room(0, f"{load} is above the high watermark {thresholds.load_high:.2f}, no room", LOAD)
    if per_cpu >= thresholds.load_low:
        held = previous or 0
        if memory < held:
            return Room(
                memory, f"{load} is between the watermarks; {memory_text}, below the previous room of {held}", MEMORY
            )
        return Room(held, f"{load} is between the watermarks, the previous room of {held} holds", LOAD)
    projected = load_room(sample, thresholds)
    if projected is not None and projected < memory:
        return Room(
            projected,
            f"{load} is below the low watermark; {sample.agents} live agents project load to the high watermark "
            f"after {projected} more",
            LOAD,
        )
    return Room(memory, f"{load} is below the low watermark; {memory_text}", MEMORY)


def _mem_available_mb(proc: Path) -> int:
    for line in (proc / "meminfo").read_text().splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) // 1024
    raise ValueError("meminfo has no MemAvailable line")


def read_host(proc: Path = Path("/proc")) -> HostSample | None:
    agents = len(account_sessions.live_sessions(proc)) + account_sessions.live_codex_sessions(proc)
    cpus = os.cpu_count() or 1
    try:
        load1 = float((proc / "loadavg").read_text().split()[0])
        available_mb = _mem_available_mb(proc)
    except (OSError, ValueError, IndexError):
        return None
    return HostSample(load1=load1, cpus=cpus, available_mb=available_mb, agents=agents)
