import math
import os
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from pathlib import Path

from hooks.context import account_sessions
from scripts.swarm.store import SwarmConfig

MEMORY_PER_AGENT_MB = 700
SWAP_FULL_SHARE = 0.9
SWAP_FLOOR_MB = 4096
MEMINFO_KEYS = frozenset({"MemAvailable", "SwapTotal", "SwapFree"})
LOAD_HIGH = 1.5
LOAD_LOW = 1.0
LOAD, MEMORY, UNKNOWN = "load", "memory", "unknown"
DEPLOYMENT_ENV = "AGENTIHOOKS_DEPLOYMENT"
NODE_OFF = (
    "host room off: a deployed controller's own node says nothing about worker capacity and no worker pool "
    "reading is wired, so spawns pass"
)
PRESSURE_OFF = (
    "host pressure alert off: a deployed controller's own node says nothing about worker capacity; "
    "the host room line shows the worker pool reading or why it is off"
)


@dataclass(frozen=True)
class Thresholds:
    load_high: float = LOAD_HIGH
    load_low: float = LOAD_LOW
    memory_per_agent_mb: int = MEMORY_PER_AGENT_MB
    swap_full_share: float = SWAP_FULL_SHARE
    swap_floor_mb: int = SWAP_FLOOR_MB


@dataclass(frozen=True)
class HostSample:
    load1: float
    cpus: int
    available_mb: int
    agents: int
    swap_used_mb: int = 0
    swap_total_mb: int = 0
    agent_mb: int = 0
    measured_agents: int = 0


@dataclass(frozen=True)
class Room:
    room: int | None
    reason: str
    limit: str = ""
    held: bool = False


def thresholds(config: SwarmConfig) -> Thresholds:
    return Thresholds(
        config.load_high, config.load_low, config.memory_per_agent_mb, config.swap_full_share, config.swap_floor_mb
    )


def spawn_room(config: SwarmConfig, sample: HostSample | Room | None, previous: int | None) -> Room:
    if isinstance(sample, Room):
        return sample
    if sample is None:
        return Room(None, "host unknown: the process files cannot be read, so spawns pass", UNKNOWN)
    return room(sample, thresholds(config), previous)


def memory_room(sample: HostSample, thresholds: Thresholds) -> int:
    if swap_full(sample, thresholds):
        return 0
    return max(0, (sample.available_mb - counted_swap_mb(sample, thresholds)) // per_agent_mb(sample, thresholds))


def counted_swap_mb(sample: HostSample, thresholds: Thresholds) -> int:
    return 0 if swap_above_share(sample, thresholds) else sample.swap_used_mb


def swap_above_share(sample: HostSample, thresholds: Thresholds) -> bool:
    return sample.swap_used_mb > thresholds.swap_full_share * sample.swap_total_mb


def swap_full(sample: HostSample, thresholds: Thresholds) -> bool:
    return swap_above_share(sample, thresholds) and sample.available_mb < thresholds.swap_floor_mb


def per_agent_mb(sample: HostSample, thresholds: Thresholds) -> int:
    return max(sample.agent_mb, thresholds.memory_per_agent_mb)


def memory_text(sample: HostSample, thresholds: Thresholds, memory: int) -> str:
    swap = (
        f"swap {sample.swap_used_mb} of {sample.swap_total_mb} MB in use is above the "
        f"{thresholds.swap_full_share:.0%} share"
    )
    if swap_full(sample, thresholds):
        return (
            f"{swap} and {sample.available_mb} MB available memory is below the {thresholds.swap_floor_mb} MB floor, "
            "no memory room"
        )
    stale = ""
    supply = (
        f"{sample.available_mb} MB available memory less {sample.swap_used_mb} of {sample.swap_total_mb} MB swap in use"
    )
    if swap_above_share(sample, thresholds):
        stale = (
            f"{swap} but {sample.available_mb} MB available memory is at or above the {thresholds.swap_floor_mb} MB "
            "floor, so the swap reads stale; "
        )
        supply = f"{sample.available_mb} MB available memory"
    figure = "the configured figure"
    if sample.measured_agents:
        figure = (
            f"the larger of {sample.agent_mb} MB measured over {sample.measured_agents} Claude agent trees "
            f"and the configured {thresholds.memory_per_agent_mb} MB"
        )
    return f"{stale}{supply} fits {memory} at {per_agent_mb(sample, thresholds)} MB each, {figure}"


def load_room(sample: HostSample, thresholds: Thresholds) -> int | None:
    if sample.agents <= 0 or sample.load1 <= 0:
        return None
    per_agent = sample.load1 / sample.agents
    return max(0, math.floor((thresholds.load_high * sample.cpus - sample.load1) / per_agent))


def room(sample: HostSample, thresholds: Thresholds = Thresholds(), previous: int | None = None) -> Room:
    per_cpu = sample.load1 / max(1, sample.cpus)
    load = f"one minute load {per_cpu:.2f} per CPU"
    memory = memory_room(sample, thresholds)
    memory_line = memory_text(sample, thresholds, memory)
    if per_cpu > thresholds.load_high:
        return Room(0, f"{load} is above the high watermark {thresholds.load_high:.2f}, no room", LOAD)
    if per_cpu >= thresholds.load_low:
        held = previous or 0
        if memory < held:
            return Room(
                memory, f"{load} is between the watermarks; {memory_line}, below the previous room of {held}", MEMORY
            )
        return Room(held, f"{load} is between the watermarks, the previous room of {held} holds", LOAD, True)
    projected = load_room(sample, thresholds)
    if projected is not None and projected < memory:
        return Room(
            projected,
            f"{load} is below the low watermark; {sample.agents} live agents project load to the high watermark "
            f"after {projected} more",
            LOAD,
        )
    return Room(memory, f"{load} is below the low watermark; {memory_line}", MEMORY)


def _memory_mb(proc: Path) -> tuple[int, int, int]:
    fields = {}
    for line in (proc / "meminfo").read_text().splitlines():
        name, _, rest = line.partition(":")
        if name in MEMINFO_KEYS:
            fields[name] = int(rest.split()[0]) // 1024
    total = fields.get("SwapTotal", 0)
    return fields["MemAvailable"], total - fields.get("SwapFree", total), total


def _rss_kb(pid: int, proc: Path) -> int | None:
    try:
        lines = (proc / str(pid) / "status").read_text().splitlines()
    except OSError:
        return None
    return next((int(line.split()[1]) for line in lines if line.startswith("VmRSS:")), None)


def _children(proc: Path) -> dict[int, list[int]]:
    children: dict[int, list[int]] = {}
    for entry in proc.iterdir():
        if entry.name.isdigit():
            children.setdefault(account_sessions._ppid(int(entry.name), proc), []).append(int(entry.name))
    return children


def agent_memory(proc: Path, roots: Collection[int]) -> tuple[int, int]:
    if not roots:
        return 0, 0
    children = _children(proc)
    trees = []
    for root in roots:
        own = _rss_kb(root, proc)
        if own is None:
            continue
        tree = list(children.get(root, []))
        for pid in tree:
            tree.extend(children.get(pid, []))
        trees.append(own + sum(_rss_kb(pid, proc) or 0 for pid in tree))
    return (sum(trees) // len(trees) // 1024, len(trees)) if trees else (0, 0)


def read_host(proc: Path = Path("/proc")) -> HostSample | None:
    sessions = account_sessions.live_sessions(proc)
    agents = len(sessions) + account_sessions.live_codex_sessions(proc)
    cpus = os.cpu_count() or 1
    try:
        load1 = float((proc / "loadavg").read_text().split()[0])
        available_mb, swap_used_mb, swap_total_mb = _memory_mb(proc)
        agent_mb, measured = agent_memory(proc, sessions)
    except (OSError, ValueError, IndexError, KeyError):
        return None
    return HostSample(
        load1=load1,
        cpus=cpus,
        available_mb=available_mb,
        agents=agents,
        swap_used_mb=swap_used_mb,
        swap_total_mb=swap_total_mb,
        agent_mb=agent_mb,
        measured_agents=measured,
    )


def deployed(environ: Mapping[str, str]) -> bool:
    return environ.get(DEPLOYMENT_ENV, "local") != "local"


def controller_host(environ: Mapping[str, str]) -> HostSample | Room | None:
    if deployed(environ):
        return Room(None, NODE_OFF, UNKNOWN)
    return read_host()
