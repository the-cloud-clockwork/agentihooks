import asyncio
from collections.abc import Callable
from typing import Any


def serve_serially(client: Any, loop: Any, subject: str, handler: Callable[[str, bytes], bytes]) -> None:
    async def respond(msg: Any) -> None:
        # ruleid: nats-serve-awaits-handler
        answer = await asyncio.get_running_loop().run_in_executor(None, handler, msg.subject, msg.data)
        await msg.respond(answer)

    loop.run(client.subscribe(subject, queue="hub-slots", cb=respond))


def serve_concurrently(client: Any, loop: Any, subject: str, handler: Callable[[str, bytes], bytes]) -> None:
    def answer(msg: Any) -> None:
        loop.run(msg.respond(handler(msg.subject, msg.data)))

    async def respond(msg: Any) -> None:
        # ok: nats-serve-awaits-handler
        asyncio.get_running_loop().run_in_executor(None, answer, msg)

    loop.run(client.subscribe(subject, queue="hub-slots", cb=respond))


async def compute(handler: Callable[[], bytes]) -> bytes:
    # ok: nats-serve-awaits-handler
    return await asyncio.get_running_loop().run_in_executor(None, handler)
