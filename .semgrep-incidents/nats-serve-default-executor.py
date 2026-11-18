import asyncio
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any


def serve_on_the_default_executor(client: Any, loop: Any, subject: str, handler: Callable[[str, bytes], bytes]) -> None:
    def answer(msg: Any) -> None:
        loop.run(msg.respond(handler(msg.subject, msg.data)))

    async def respond(msg: Any) -> None:
        # ruleid: nats-serve-default-executor
        asyncio.get_running_loop().run_in_executor(None, answer, msg)

    loop.run(client.subscribe(subject, queue="hub-slots", cb=respond))


def serve_on_a_bounded_pool(
    client: Any, loop: Any, pool: ThreadPoolExecutor, subject: str, handler: Callable[[str, bytes], bytes]
) -> None:
    def answer(msg: Any, received: float) -> None:
        if time.monotonic() - received < 10:
            loop.run(msg.respond(handler(msg.subject, msg.data)))

    async def respond(msg: Any) -> None:
        # ok: nats-serve-default-executor
        pool.submit(answer, msg, time.monotonic())

    loop.run(client.subscribe(subject, queue="hub-slots", cb=respond))


async def compute(handler: Callable[[], bytes]) -> bytes:
    # ok: nats-serve-default-executor
    return await asyncio.get_running_loop().run_in_executor(None, handler)
