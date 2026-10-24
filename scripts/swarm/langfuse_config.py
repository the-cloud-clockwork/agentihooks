from dataclasses import dataclass


@dataclass(frozen=True)
class Limits:
    interval_ms: int = 600000
    window_ms: int = 3600000
    error_rate: float = 0.2
    min_observations: int = 10
    read_timeout_seconds: float = 30
