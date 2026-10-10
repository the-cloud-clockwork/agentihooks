from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from mcp.server.lowlevel import Server
from mcp.types import CallToolResult, TextContent, Tool

from hooks.serena_router.binding import (
    PRIMARY,
    Binding,
    BindingError,
    ensure_excluded,
    inherit_project_config,
    resolve,
)
from hooks.serena_router.pool import BackendError, Pool

ACTIVATE = "activate_project"
# Claude covers these with its own tools; a client lists them by naming them in the endpoint's `tools` query.
OPT_IN = frozenset({"read_file", "search_for_pattern"})

UNBOUND = (
    "No project is active for this session. The router may have restarted. "
    "Call activate_project with the absolute path of your worktree, then retry."
)


def _text(message: str, *, error: bool = False) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=message)], isError=error)


def edit_tools(tools: list[Tool]) -> frozenset[str]:
    return frozenset(t.name for t in tools if not (t.annotations and t.annotations.readOnlyHint))


def listed(tools: list[Tool], values: list[str]) -> list[Tool]:
    named = {name for value in values for name in value.split(",")}
    return [t for t in tools if t.name not in OPT_IN - named]


async def activate(pool: Pool, binding: Binding, project: str) -> CallToolResult:
    try:
        root, kind = resolve(project)
        inherit_project_config(root)
        ensure_excluded(root)
        await pool.get(root)
    except (BindingError, BackendError) as exc:
        return _text(str(exc), error=True)
    binding.root, binding.kind = root, kind
    if kind == PRIMARY:
        return _text(f"Active project: {root} (primary checkout: reads only, edits are refused).")
    return _text(f"Active project: {root} (worktree).")


async def dispatch(pool: Pool, binding: Binding, edits: frozenset[str], name: str, arguments: dict) -> CallToolResult:
    if name == ACTIVATE:
        return await activate(pool, binding, str(arguments.get("project", "")))
    if binding.root is None:
        return _text(UNBOUND, error=True)
    if not binding.root.is_dir():
        released, binding.root, binding.kind = binding.root, None, ""
        return _text(f"{released} no longer exists. {UNBOUND}", error=True)
    if name in edits and binding.kind == PRIMARY:
        return _text(
            f"Refused: {name} edits files and this session is bound to the primary checkout {binding.root}. "
            "Activate the absolute path of your worktree first.",
            error=True,
        )
    try:
        backend = await pool.get(binding.root)
        result = await backend.call(name, arguments)
    except BackendError as exc:
        return _text(str(exc), error=True)
    if name in edits:
        result.content.append(TextContent(type="text", text=f"(applied in {binding.root})"))
    return result


def build_server(pool: Pool, tools: list[Tool]) -> Server:
    edits = edit_tools(tools)
    # The router answers activation and refusals itself, without structured content a declared schema would demand.
    tools = [t.model_copy(update={"outputSchema": None}) for t in tools]

    @asynccontextmanager
    async def lifespan(_server: Server) -> AsyncIterator[Binding]:
        binding = Binding()
        pool.bindings.add(binding)
        yield binding

    server: Server = Server("serena", lifespan=lifespan)

    @server.list_tools()
    async def _list_tools() -> list[Tool]:
        request = server.request_context.request
        return listed(tools, request.query_params.getlist("tools") if request else [])

    @server.call_tool(validate_input=False)
    async def _call_tool(name: str, arguments: dict | None) -> CallToolResult:
        binding = server.request_context.lifespan_context
        return await dispatch(pool, binding, edits, name, arguments or {})

    return server
