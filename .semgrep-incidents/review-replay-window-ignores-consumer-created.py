from typing import Any

REPLAY_MS = 6 * 3_600_000


def fresh_by_the_window(delivery: Any, now_ms: int) -> bool:
    # ruleid: review-replay-window-ignores-consumer-created
    return delivery.event.time >= now_ms - REPLAY_MS


def stale_by_the_window(delivery: Any, now_ms: int) -> bool:
    # ruleid: review-replay-window-ignores-consumer-created
    return delivery.event.time < now_ms - REPLAY_MS


def aged_within_the_window(delivery: Any, now_ms: int) -> bool:
    # ruleid: review-replay-window-ignores-consumer-created
    return now_ms - delivery.event.time <= REPLAY_MS


def aged_past_the_window(delivery: Any, now_ms: int) -> bool:
    # ruleid: review-replay-window-ignores-consumer-created
    return now_ms - delivery.event.time > REPLAY_MS


def floor(created: int | None, now_ms: int) -> int:
    window = now_ms - REPLAY_MS
    return window if created is None else min(created, window)


def fresh_by_the_window_or_the_consumer(delivery: Any, consumer: Any, now_ms: int) -> bool:
    # ok: review-replay-window-ignores-consumer-created
    return delivery.event.time >= floor(consumer.created, now_ms)
