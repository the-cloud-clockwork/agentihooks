from concurrent.futures import Future, wait
from dataclasses import dataclass, field
from typing import Any

from scripts.tick.steps import host as host_steps
from scripts.tick.steps.host import pass_view


@dataclass
class _Wrapper:
    box: dict[str, Any] = field(default_factory=dict[str, Any])
    judging: dict[str, Any] = field(default_factory=dict[str, Any])
    slug: str = "sw"

    def _judged(self) -> bool:
        view, _ = self.judging["judged"]
        # ruleid: tick-site-hook-waits-on-future
        self.box["view"] = host_steps.pass_view(self.slug, view)
        return True

    def _viewed(self) -> bool:
        view, _ = self.judging["judged"]
        # ruleid: tick-site-hook-waits-on-future
        self.box["view"] = pass_view(self.slug, view)
        return True

    def _built(self) -> bool:
        build: Future[Any] = self.judging["judged"][0]
        # ruleid: tick-site-hook-waits-on-future
        self.box["view"] = build.result(timeout=40.0)
        return True

    def _awaited(self) -> bool:
        build: Future[Any] = self.judging["judged"][0]
        # ruleid: tick-site-hook-waits-on-future
        wait((build,), timeout=40.0)
        return True

    def _kept(self) -> bool:
        if "judged" in self.judging:
            # ok: tick-site-hook-waits-on-future
            _, self.box["checked"] = self.judging["judged"]
        return True


class TickSites(dict[str, Any]):
    def _listed(self) -> bool:
        build: Future[Any] = self["build"]
        # ruleid: tick-site-hook-waits-on-future
        self["view"] = build.result()
        return True


def _view_wait(get: Any) -> None:
    view, _ = get("judging")["judged"]
    # ok: tick-site-hook-waits-on-future
    get("box")["view"] = host_steps.pass_view(get("slug"), view)
