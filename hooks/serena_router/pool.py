from __future__ import annotations

import asyncio
import os
import sys
import time
import weakref
from dataclasses import dataclass, field
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import CallToolResult, Tool

from hooks.serena_router.binding import Binding


class BackendError(RuntimeError):
    pass


@dataclass(frozen=True)
class BackendConfig:
    command: tuple[str, ...]
    env: dict[str, str] = field(default_factory=dict)
    idle_seconds: float = 1800.0
    max_backends: int = 6
    start_timeout: float = 180.0

    def params(self, root: Path | None) -> StdioServerParameters:
        args = list(self.command[1:])
        if root is not None:
            args += ["--project", str(root)]
        return StdioServerParameters(
            command=self.command[0],
            args=args,
            env={**os.environ, **self.env},
            cwd=str(root) if root is not None else None,
        )


class Backend:
    def __init__(self, root: Path | None, config: BackendConfig) -> None:
        self.root = root
        self.config = config
        self.session: ClientSession | None = None
        self.error = ""
        self.started_at = time.time()
        self.last_used = time.monotonic()
        self.calls = 0
        self._ready = asyncio.Event()
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run())
        try:
            await asyncio.wait_for(self._ready.wait(), self.config.start_timeout)
        except TimeoutError:
            self.error = f"no answer within {self.config.start_timeout:.0f}s"
        if self.session is None:
            await self.stop()
            raise BackendError(f"Serena failed to start for {self.root}: {self.error}")

    async def _run(self) -> None:
        try:
            async with stdio_client(self.config.params(self.root), errlog=sys.__stderr__) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    self.session = session
                    self._ready.set()
                    await self._stop.wait()
        except Exception as exc:  # noqa: BLE001 - surfaced through status and the start error
            self.error = repr(exc)
        finally:
            self.session = None
            self._ready.set()

    async def call(self, name: str, arguments: dict) -> CallToolResult:
        if self.session is None:
            raise BackendError(f"Serena for {self.root} is not running: {self.error}")
        self.last_used = time.monotonic()
        self.calls += 1
        return await self.session.call_tool(name, arguments)

    async def list_tools(self) -> list[Tool]:
        if self.session is None:
            raise BackendError(f"Serena is not running: {self.error}")
        return (await self.session.list_tools()).tools

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            await asyncio.gather(self._task, return_exceptions=True)

    @property
    def alive(self) -> bool:
        return self.session is not None


class Pool:
    def __init__(self, config: BackendConfig) -> None:
        self.config = config
        self.bindings: weakref.WeakSet[Binding] = weakref.WeakSet()
        self._backends: dict[Path, Backend] = {}
        self._start_lock = asyncio.Lock()

    async def get(self, root: Path) -> Backend:
        backend = self._backends.get(root)
        if backend is not None and backend.alive:
            return backend
        async with self._start_lock:
            backend = self._backends.get(root)
            if backend is not None and backend.alive:
                return backend
            await self._make_room()
            backend = Backend(root, self.config)
            await backend.start()
            self._backends[root] = backend
            return backend

    async def _make_room(self) -> None:
        live = [b for b in self._backends.values() if b.alive]
        while len(live) >= self.config.max_backends:
            oldest = min(live, key=lambda b: b.last_used)
            await self.release(oldest.root)
            live.remove(oldest)

    async def release(self, root: Path | None) -> bool:
        backend = self._backends.pop(root, None) if root is not None else None
        if backend is None:
            return False
        await backend.stop()
        return True

    async def evict_idle(self) -> list[Path]:
        cutoff = time.monotonic() - self.config.idle_seconds
        idle = [root for root, b in self._backends.items() if b.last_used < cutoff or not b.alive]
        for root in idle:
            await self.release(root)
        return idle

    async def close(self) -> None:
        for root in list(self._backends):
            await self.release(root)

    def snapshot(self) -> list[dict]:
        now = time.monotonic()
        rows = []
        for root, backend in sorted(self._backends.items()):
            rows.append(
                {
                    "root": str(root),
                    "alive": backend.alive,
                    "sessions": sum(1 for b in self.bindings if b.root == root),
                    "calls": backend.calls,
                    "idle_seconds": round(now - backend.last_used),
                    "started_at": backend.started_at,
                    "error": backend.error,
                }
            )
        return rows


async def probe_tools(config: BackendConfig) -> list[Tool]:
    backend = Backend(None, config)
    await backend.start()
    try:
        return await backend.list_tools()
    finally:
        await backend.stop()
