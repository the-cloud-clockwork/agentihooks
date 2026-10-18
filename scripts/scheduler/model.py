from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class Schedule:
    kind: str
    owner: str
    payload: str
    due: float
    delay: float | None
    expression: str = ""
    timezone: str = "UTC"


def next_due(expression: str, timezone: str, now: float) -> float:
    from croniter import croniter

    if len(expression.split()) != 5:
        raise ValueError("a cron expression needs five fields")
    return croniter(expression, datetime.fromtimestamp(now, ZoneInfo(timezone))).get_next(datetime).timestamp()
