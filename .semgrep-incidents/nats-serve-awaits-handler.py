import asyncio
from collections.abc import Callable
from typing import Any


class Serial:
    def serve(self, subject: str, queue: str, handler: Callable[[str, bytes], bytes]) -> None:
        async def respond(msg: Any) -> None:
            # ruleid: nats-serve-awaits-handler
            answer = await asyncio.get_running_loop().run_in_executor(None, handler, msg.subject, msg.data)
            await msg.respond(answer)

        self._loop.run(self._nc.subscribe(subject, queue=queue, cb=respond))


class Concurrent:
    def serve(self, subject: str, queue: str, handler: Callable[[str, bytes], bytes]) -> None:
        def answer(msg: Any) -> None:
            self._loop.run(msg.respond(handler(msg.subject, msg.data)))

        async def respond(msg: Any) -> None:
            # ok: nats-serve-awaits-handler
            asyncio.get_running_loop().run_in_executor(None, answer, msg)

        self._loop.run(self._nc.subscribe(subject, queue=queue, cb=respond))


class NotACallback:
    async def compute(self, handler: Callable[[], bytes]) -> bytes:
        # ok: nats-serve-awaits-handler
        return await asyncio.get_running_loop().run_in_executor(None, handler)
