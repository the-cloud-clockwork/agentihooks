from __future__ import annotations

import asyncio
import html
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.types import Tool
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse
from starlette.routing import Route
from starlette.types import Receive, Scope, Send

from hooks.serena_router.pool import Pool
from hooks.serena_router.server import build_server

SESSION_IDLE_SECONDS = 4 * 3600
EVICT_EVERY_SECONDS = 60


class _MCPEndpoint:
    def __init__(self, manager: StreamableHTTPSessionManager) -> None:
        self.manager = manager

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self.manager.handle_request(scope, receive, send)


async def _evict_forever(pool: Pool) -> None:
    while True:
        await asyncio.sleep(EVICT_EVERY_SECONDS)
        await pool.evict_idle()


def _status_html(rows: list[dict]) -> str:
    cells = "".join(
        "<tr>"
        f"<td>{html.escape(r['root'])}</td><td>{'up' if r['alive'] else 'down'}</td>"
        f"<td>{r['sessions']}</td><td>{r['calls']}</td><td>{r['idle_seconds']}s</td>"
        f"<td>{html.escape(r['error'])}</td>"
        "</tr>"
        for r in rows
    )
    return (
        "<!doctype html><meta charset=utf-8><title>Serena router</title>"
        "<style>body{font:14px system-ui;margin:24px}td,th{padding:4px 12px;text-align:left}</style>"
        f"<h1>Serena router</h1><p>{len(rows)} backend(s)</p>"
        "<table><tr><th>Worktree</th><th>State</th><th>Sessions</th><th>Calls</th><th>Idle</th><th>Error</th></tr>"
        f"{cells}</table>"
    )


def build_app(pool: Pool, tools: list[Tool]) -> Starlette:
    manager = StreamableHTTPSessionManager(app=build_server(pool, tools), session_idle_timeout=SESSION_IDLE_SECONDS)

    async def healthz(_request: Request) -> PlainTextResponse:
        return PlainTextResponse("ok")

    async def status_json(_request: Request) -> JSONResponse:
        return JSONResponse({"backends": pool.snapshot()})

    async def status_page(_request: Request) -> HTMLResponse:
        return HTMLResponse(_status_html(pool.snapshot()))

    async def release(request: Request) -> JSONResponse:
        body = await request.json()
        path = Path(str(body.get("path", ""))).resolve()
        return JSONResponse({"released": await pool.release(path)})

    @asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        async with manager.run():
            evictor = asyncio.create_task(_evict_forever(pool))
            try:
                yield
            finally:
                evictor.cancel()
                await pool.close()

    app = Starlette(
        routes=[
            Route("/mcp", endpoint=_MCPEndpoint(manager)),
            Route("/healthz", healthz),
            Route("/status.json", status_json),
            Route("/release", release, methods=["POST"]),
            Route("/", status_page),
        ],
        lifespan=lifespan,
    )
    app.state.session_manager = manager
    return app
