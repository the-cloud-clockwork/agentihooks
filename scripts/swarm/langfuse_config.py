from dataclasses import dataclass


@dataclass(frozen=True)
class Limits:
    interval_ms: int = 600000
    window_ms: int = 3600000
    error_rate: float = 0.2
    min_observations: int = 10
    turn_latency_ms: float = 600000
    min_turns: int = 3
    read_timeout_seconds: float = 30
