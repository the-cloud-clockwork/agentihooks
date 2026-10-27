import json
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import asdict, dataclass

HOUR_MS = 3_600_000
MINUTE_MS = 60_000
LEDGER_CHECKS = ("ledger_join", "ledger_write")
COUNTED = ("started", "failed")
UNKNOWN = "unknown"
WORKSTATION = "workstation"
ROOT = "root"
RELEASE = ["api", "repos/{owner}/{repo}/releases/latest"]


@dataclass(frozen=True)
class Thresholds:
    launch_window_ms: int = 6 * HOUR_MS
    launch_success_min: float = 0.9
    task_list_samples: int = 3
    task_list_reads_min: float = 12.0
    merge_stall_ms: int = HOUR_MS
    release_age_max_ms: int = 24 * HOUR_MS


THRESHOLDS = Thresholds()


@dataclass(frozen=True)
class Probe:
    name: str
    passed: bool
    measured: str

    def line(self) -> str:
        return f"{'pass' if self.passed else 'fail'}\t{self.name}\t{self.measured}"


def _named(names: list[str]) -> str:
    return f": {', '.join(names)}" if names else ""


def hives(records: list[dict], now_ms: int) -> Probe:
    from scripts.hive import registry

    stale = [record["id"] for record in records if not registry.live(record, now_ms)]
    return Probe("every hive live", not stale, f"{len(records) - len(stale)} of {len(records)} live{_named(stale)}")


def hive_joins(reports: list[dict]) -> Probe:
    failed = [
        report["agent"] for report in reports if not all(report["checks"].get(check) is True for check in LEDGER_CHECKS)
    ]
    passed = len(reports) - len(failed)
    return Probe(
        "every hive agent joins and writes the ledger", not failed, f"{passed} of {len(reports)} passed{_named(failed)}"
    )


def placements(agents: list[dict]) -> dict[str, tuple[str, str]]:
    return {
        agent["name"]: (
            agent.get("hive") or agent.get("runtime_target", {}).get("host_id") or WORKSTATION,
            agent.get("harness") or UNKNOWN,
        )
        for agent in agents
    }


def launches(rows: list[dict], placed: dict, now_ms: int, limits: Thresholds) -> list[Probe]:
    started, total = Counter(), Counter()
    for row in rows:
        if row["state"] in COUNTED and now_ms - row["at"] <= limits.launch_window_ms:
            where = placed.get(row["agent"], (UNKNOWN, UNKNOWN))
            total[where] += 1
            started[where] += row["state"] == "started"
    hours = f"{limits.launch_window_ms / HOUR_MS:g}"
    if not total:
        return [Probe("launch success", True, f"no launches in the last {hours} hours")]
    return [
        Probe(
            f"launch success hive {hive} harness {harness}",
            started[(hive, harness)] / total[(hive, harness)] >= limits.launch_success_min,
            f"{started[(hive, harness)] / total[(hive, harness)]:.0%}, {started[(hive, harness)]} of "
            f"{total[(hive, harness)]} started in the last {hours} hours",
        )
        for hive, harness in sorted(total)
    ]


def task_list_reads(read: Callable[[], object], limits: Thresholds, clock: Callable[[], float]) -> Probe:
    started = clock()
    for _ in range(limits.task_list_samples):
        read()
    rate = limits.task_list_samples * 60 / (clock() - started)
    return Probe(
        "full task list reads per minute",
        rate >= limits.task_list_reads_min,
        f"{rate:.1f} per minute over {limits.task_list_samples} reads",
    )


def workstation(running: int, cap: int, local: int) -> Probe:
    return Probe(
        "workstation agents at or under the root cap",
        running <= cap,
        f"{running} running, root cap {cap}, swarm local limit {local}",
    )


def merge_queue(observed: dict, limits: Thresholds) -> Probe:
    if not observed["queue"]:
        return Probe("merge queue groups moving", True, "queue empty")
    idle = observed["at"] - observed["merged"]
    return Probe(
        "merge queue groups moving",
        idle < limits.merge_stall_ms,
        f"{observed['queue']} queued, last merge {idle // MINUTE_MS} minutes ago",
    )


def release(published_ms: int, now_ms: int, limits: Thresholds) -> Probe:
    age = now_ms - published_ms
    return Probe("release age", age <= limits.release_age_max_ms, f"{age / HOUR_MS:.1f} hours")


def guarded(name: str, read: Callable[[], list[Probe]]) -> list[Probe]:
    try:
        return read()
    except Exception as exc:
        return [Probe(name, False, f"unread: {type(exc).__name__}: {exc}")]


def run(store, slug: str, ledger, now_ms: int, limits: Thresholds = THRESHOLDS) -> list[Probe]:
    from scripts.hive import join_report, registry
    from scripts.swarm import local_slots
    from scripts.swarm.commands import hive_id
    from scripts.swarm.health import merge_stall
    from scripts.swarm.publish import local_repository

    config = store.config(slug)

    def repo():
        return local_repository(config.repo, config.repo)

    def joined() -> list[Probe]:
        live = [record["id"] for record in registry.hives(store.redis) if registry.live(record, now_ms)]
        reports = [report for hive in live for report in join_report.live(store.redis, hive)]
        return [hive_joins([report for report in reports if report["slug"] == slug])]

    def launched() -> list[Probe]:
        history = [json.loads(raw) for raw in store.redis.lrange(store.key(slug, "history"), 0, -1)]
        agents = [*history, *(asdict(agent) for agent in store.agents(slug))]
        return launches(store.launches(slug), placements(agents), now_ms, limits)

    def room() -> list[Probe]:
        cap = registry.show(store.redis, ROOT)["max_agents"]
        running = local_slots.occupancy(store, ROOT)[0] if hive_id() == ROOT else local_slots.stored(store, ROOT)
        return [workstation(running, cap, config.local_max_agents)]

    def released() -> list[Probe]:
        latest = merge_stall.request(str(repo()), RELEASE)
        return [release(merge_stall.timestamp(latest["published_at"]), now_ms, limits)]

    readers = (
        ("every hive live", lambda: [hives(registry.hives(store.redis), now_ms)]),
        ("every hive agent joins and writes the ledger", joined),
        ("launch success", launched),
        (
            "full task list reads per minute",
            lambda: [task_list_reads(lambda: ledger.tasks(slug), limits, time.monotonic)],
        ),
        ("workstation agents at or under the root cap", room),
        ("merge queue groups moving", lambda: [merge_queue(merge_stall.observe(str(repo()), now_ms), limits)]),
        ("release age", released),
    )
    return [probe for name, read in readers for probe in guarded(name, read)]
