"""Host port adapter: named host readings and background host jobs, each call bounded by a deadline."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

from scripts.tick.adapters.inbox import check
from scripts.tick.deadline import Deadline
from scripts.tick.ports import Unsupported

if TYPE_CHECKING:
    from scripts.swarm.store import RedisStore

__all__ = ["JobHost", "local_host"]


@dataclass(frozen=True)
class JobHost:
    readers: Mapping[str, Callable[[], object]]
    jobs: Mapping[str, Callable[[Mapping[str, object]], object]]

    def read(self, name: str, *, deadline: Deadline) -> object:
        check(deadline)
        reader = self.readers.get(name)
        return Unsupported(name) if reader is None else reader()

    def run(self, request: Mapping[str, object], *, deadline: Deadline) -> object:
        check(deadline)
        name = str(request.get("job", ""))
        job = self.jobs.get(name)
        return Unsupported(name) if job is None else job(request)


def local_host(store: "RedisStore", slug: str, environ: Mapping[str, str]) -> JobHost:
    """Wire the controller host sample and the refresh and snapshot jobs of one swarm."""
    from scripts.swarm import host_budget, refresh_job, snapshot_job

    return JobHost(
        {"sample": lambda: host_budget.controller_host(environ)},
        {
            "refresh": lambda request: refresh_job.start(store, slug),  # pyright: ignore[reportUnknownMemberType]
            "snapshot": lambda request: snapshot_job.start(store, slug, request["now_ms"], environ),  # pyright: ignore[reportUnknownMemberType]
        },
    )
