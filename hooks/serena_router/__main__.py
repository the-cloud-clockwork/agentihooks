from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path

import uvicorn

from hooks.serena_router.app import build_app
from hooks.serena_router.pool import BackendConfig, Pool, probe_tools

DEFAULT_PORT = 8643


def default_context() -> str:
    for candidate in (
        os.environ.get("AGENTIHOOKS_SERENA_CONTEXT", ""),
        str(Path.home() / ".serena" / "contexts" / "claude-code-worktrees.yml"),
    ):
        if candidate and Path(candidate).is_file():
            return candidate
    return "claude-code"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m hooks.serena_router")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--serena", default="serena")
    parser.add_argument("--context", default=None)
    parser.add_argument("--idle-seconds", type=float, default=1800.0)
    parser.add_argument("--max-backends", type=int, default=6)
    return parser.parse_args(argv)


def backend_config(args: argparse.Namespace) -> BackendConfig:
    command = (
        args.serena,
        "start-mcp-server",
        "--context",
        args.context or default_context(),
        "--transport",
        "stdio",
        "--enable-web-dashboard",
        "False",
    )
    return BackendConfig(
        command=command,
        env={"SERENA_USAGE_REPORTING": "false"},
        idle_seconds=args.idle_seconds,
        max_backends=args.max_backends,
    )


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    config = backend_config(args)
    tools = asyncio.run(probe_tools(config))
    uvicorn.run(build_app(Pool(config), tools), host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
