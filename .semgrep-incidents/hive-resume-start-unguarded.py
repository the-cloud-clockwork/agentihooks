from typing import Any


def start_resume(consumer: Any, order: Any, receipt: str) -> None:
    redis = consumer.orders.redis
    if not redis.set(f"{receipt}:running", "1", nx=True, ex=120):
        return
    redis.expire(receipt, 86400)
    # ruleid: hive-resume-start-unguarded
    consumer.offbeat(order)


def start_resume_answering_failure(consumer: Any, order: Any, receipt: str) -> None:
    redis = consumer.orders.redis
    if not redis.set(f"{receipt}:running", "1", nx=True, ex=120):
        return
    try:
        # ok: hive-resume-start-unguarded
        consumer.offbeat(order)
    except Exception as exc:
        consumer.answer_failed(receipt, f"error: {type(exc).__name__}")
