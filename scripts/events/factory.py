import importlib
from collections.abc import Mapping

from .bus import Bus
from .config import BusConfig

BACKENDS = {
    "memory": ("scripts.events.memory", "open_bus"),
    "nats": ("scripts.events.nats", "open_bus"),
}


def open_bus(config: BusConfig, registry: Mapping[str, tuple[str, str]] = BACKENDS) -> Bus:
    target = registry.get(config.backend)
    if target is None:
        raise ValueError(f"unknown bus backend {config.backend!r}; known: {', '.join(sorted(registry))}")
    module, function = target
    return getattr(importlib.import_module(module), function)(config)
