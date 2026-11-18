from hooks.serena_router.binding import ROOT_ARG, Binding, BindingError
from hooks.serena_router.pool import Pool
from hooks.serena_router.server import _text, carried_binding


async def route_by_the_session_alone(pool: Pool, binding: Binding, name: str, arguments: dict) -> object:
    backend = await pool.get(binding.root)
    # ruleid: serena-router-routes-by-the-session-binding-alone
    return await backend.call(name, arguments)


async def resolve_the_carried_root_but_forward_it(pool: Pool, binding: Binding, name: str, arguments: dict) -> object:
    binding = await carried_binding(binding, arguments)
    backend = await pool.get(binding.root)
    # ruleid: serena-router-routes-by-the-session-binding-alone
    return await backend.call(name, arguments)


async def strip_the_carried_root_unread(pool: Pool, binding: Binding, name: str, arguments: dict) -> object:
    arguments = {key: value for key, value in arguments.items() if key != ROOT_ARG}
    backend = await pool.get(binding.root)
    # ruleid: serena-router-routes-by-the-session-binding-alone
    return await backend.call(name, arguments)


async def resolve_the_carried_root_but_route_by_the_session(
    pool: Pool, binding: Binding, session: Binding, name: str, arguments: dict
) -> object:
    binding = await carried_binding(binding, arguments)
    arguments = {key: value for key, value in arguments.items() if key != ROOT_ARG}
    backend = await pool.get(session.root)
    # ruleid: serena-router-routes-by-the-session-binding-alone
    return await backend.call(name, arguments)


async def route_by_the_carried_root(pool: Pool, binding: Binding, name: str, arguments: dict) -> object:
    binding = await carried_binding(binding, arguments)
    arguments = {key: value for key, value in arguments.items() if key != ROOT_ARG}
    backend = await pool.get(binding.root)
    # ok: serena-router-routes-by-the-session-binding-alone
    return await backend.call(name, arguments)


async def route_by_the_carried_root_or_refuse(pool: Pool, binding: Binding, name: str, arguments: dict) -> object:
    try:
        binding = await carried_binding(binding, arguments)
    except BindingError as exc:
        return _text(str(exc), error=True)
    arguments = {key: value for key, value in arguments.items() if key != ROOT_ARG}
    if binding.root is None:
        return _text("unbound", error=True)
    try:
        backend = await pool.get(binding.root)
        # ok: serena-router-routes-by-the-session-binding-alone
        result = await backend.call(name, arguments)
    except BindingError as exc:
        return _text(str(exc), error=True)
    return result
