from typing import Any

UNKNOWN = "unknown"


def live(store: Any, agent: Any) -> bool:
    return bool(store and agent)


class Unguarded:
    def _halt(self, slug: str, agent: Any) -> str:
        if self._stop(slug, agent) != UNKNOWN:
            return "stopped"
        # ruleid: hub-hang-frees-live-agent
        self._freer().free(slug, agent)
        return "freed"


class GuardedAfter:
    def _halt(self, slug: str, agent: Any) -> str:
        # ruleid: hub-hang-frees-live-agent
        self._freer().free(slug, agent)
        if live(self._store, agent):
            return "kept"
        return "freed"


class Guarded:
    def _halt(self, slug: str, agent: Any) -> str:
        if self._stop(slug, agent) != UNKNOWN:
            return "stopped"
        if live(self._connected(), agent):
            return "kept"
        # ok: hub-hang-frees-live-agent
        self._freer().free(slug, agent)
        return "freed"


class GuardedInside:
    def _halt(self, slug: str, agent: Any) -> str:
        if not live(self._connected(), agent):
            # ok: hub-hang-frees-live-agent
            self._admission.free(slug, agent)
            return "freed"
        return "kept"
